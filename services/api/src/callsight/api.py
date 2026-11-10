"""HTTP API. Thin wrapper over callsight.service; requires fastapi (see pyproject.toml).

Every /v1 route that touches stored data needs an API key (Authorization: Bearer cs_..., or
X-API-Key), created with `python -m callsight.admin create-key`. A key belongs to one
organization and only ever sees that organization's calls. Webhooks authenticate with their
org's HMAC secret instead. Without a database, POST /v1/analyze is a stateless, open endpoint.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import anyio
from minio.error import MinioException
from urllib3.exceptions import HTTPError as Urllib3Error
from dataclasses import replace
from pathlib import Path, PurePath
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, StringConstraints

from starlette.middleware.sessions import SessionMiddleware

from . import __version__, auth, recordings, remote, retention, sso, store
from .auth import Principal
from .config import Settings
from .models import Transcript
from .connectors import Unauthorized
from .migrate import migrate
from .recordings import AUDIO_TYPES
from .registry import available, create, load_builtins
from .service import analyze_text_with_transcript, analyze_transcript


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = Settings.from_env()
    if settings.database_url:
        migrate(settings.database_url)
    yield


MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # about 8 hours of 128 kbps mp3
MAX_BODY_BYTES = 10 * 1024 * 1024  # every other request; a long call's transcript is well under 1 MB
UPLOAD_PATH = "/v1/calls/upload"


class BodySizeLimit:
    """Reject oversized request bodies while they arrive, before they are spooled to disk:
    by Content-Length when the client sends one, otherwise by counting bytes as they stream in."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = MAX_UPLOAD_BYTES if scope["path"] == UPLOAD_PATH else MAX_BODY_BYTES
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > limit:
            response = JSONResponse({"detail": f"Request body larger than {limit // 2**20} MB"}, status_code=413)
            return await response(scope, receive, send)
        if scope["path"] == UPLOAD_PATH:
            # FastAPI parses the body before running dependencies, so without this an anonymous
            # client could make the server read and spool a 500 MB upload before being rejected.
            try:
                override = app.dependency_overrides.get(require_key)  # tests stub auth this way
                if override is not None:
                    override()
                else:
                    await anyio.to_thread.run_sync(require_key, Request(scope))
            except HTTPException as exc:
                response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
                return await response(scope, receive, send)
        received = 0

        async def counting_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:  # FastAPI re-raises HTTPException from body parsing as-is
                    raise HTTPException(status_code=413, detail=f"Request body larger than {limit // 2**20} MB")
            return message

        await self.app(scope, counting_receive, send)


app = FastAPI(title="Callsight", version=__version__, lifespan=lifespan)
app.add_middleware(BodySizeLimit)
# Added last = outermost, so the session is decoded before BodySizeLimit authenticates uploads.
_startup_settings = Settings.from_env()
app.add_middleware(
    SessionMiddleware,
    secret_key=auth._link_secret(_startup_settings.secret_key).decode(),
    session_cookie="callsight_session",
    max_age=sso.SESSION_HOURS * 3600,
    same_site="lax",
    https_only=_startup_settings.public_url.startswith("https://"),
)
app.include_router(sso.router)
log = logging.getLogger("callsight.api")
if sso.enabled(_startup_settings) and not _startup_settings.secret_key:
    log.warning("CALLSIGHT_SECRET_KEY is not set: sign-in sessions will end whenever the API restarts")


@app.exception_handler(MinioException)
@app.exception_handler(Urllib3Error)
async def storage_unavailable(_request: Request, exc: Exception) -> JSONResponse:
    """Object storage down, full or misconfigured: a clear 503 instead of a stack trace."""
    log.error("recording storage error: %s", exc)
    return JSONResponse({"detail": "Recording storage is unavailable; try again later"}, status_code=503)
_STATIC = Path(__file__).parent / "static"


class AnalyzeRequest(BaseModel):
    call_id: str = "adhoc"
    transcript: str = Field(description="Plain text, one turn per line: '[mm:ss] Speaker: text'")
    competitors: list[str] = Field(default_factory=list)


def _database_url(settings: Settings) -> str:
    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Persistence is disabled: CALLSIGHT_DATABASE_URL is not set")
    return settings.database_url


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def require_key(request: Request) -> Principal:
    """The organization behind the request's API key, or behind the signed-in user's session.
    401 without either."""
    header = request.headers.get("authorization", "")
    key = header[7:].strip() if header.lower().startswith("bearer ") else request.headers.get("x-api-key")
    if not key:
        principal = sso.session_principal(request)
        if principal is None:
            raise HTTPException(status_code=401, detail="Sign in or send an API key", headers={"WWW-Authenticate": "Bearer"})
        # Cookies ride along on cross-site requests; a custom header cannot be sent cross-site without
        # a CORS preflight (which we never allow), so requiring it blocks CSRF on session-authenticated writes.
        if request.method not in SAFE_METHODS and request.headers.get("x-callsight-csrf") != "1":
            raise HTTPException(status_code=403, detail="Missing X-Callsight-CSRF header")
        return principal
    with store.connect(_database_url(Settings.from_env())) as conn:
        principal = auth.principal_for_key(conn, key)
    if principal is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key", headers={"WWW-Authenticate": "Bearer"})
    return principal


@app.get("/", include_in_schema=False)
def ui() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


@app.get("/auth/me")
def whoami(principal: Principal = Depends(require_key)) -> dict:
    """Who this request is authenticated as: 'user:<email>' for sign-in, 'key:<name>' for API keys."""
    return {"actor": principal.actor, "org_id": principal.org_id}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.post("/v1/analyze")
def analyze_call(request: AnalyzeRequest, http: Request) -> dict:
    """Analyze a transcript. With a database configured, an API key is required and the call is stored."""
    settings = Settings.from_env()
    principal = require_key(http) if settings.database_url else None
    competitors = list(request.competitors)
    if principal is not None:
        with store.connect(settings.database_url) as conn:
            competitors += store.org_competitors(conn, principal.org_id)
    try:
        transcript, report = analyze_text_with_transcript(
            request.transcript, call_id=request.call_id, settings=settings, extra_competitors=competitors
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    result = report.to_dict()
    if principal is not None:
        with store.connect(settings.database_url) as conn:
            result["id"] = store.save_call(conn, principal.org_id, transcript, report)
            store.audit(conn, principal.org_id, principal.actor, "call.create", str(result["id"]), {"source": "text"})
    return result


@app.post("/v1/calls/upload", status_code=202)
def upload_call(
    file: UploadFile = File(...), call_id: str = Form("upload"), principal: Principal = Depends(require_key)
) -> dict:
    """Store a recording and queue it for transcription and analysis by the worker."""
    settings = Settings.from_env()
    if not settings.s3_endpoint:
        raise HTTPException(status_code=503, detail="Audio upload is disabled: CALLSIGHT_S3_ENDPOINT is not set")
    ext = PurePath(file.filename or "").suffix.lower()
    if ext not in AUDIO_TYPES:
        raise HTTPException(status_code=415, detail=f"Unsupported file type; expected one of {sorted(AUDIO_TYPES)}")
    if not file.size:
        raise HTTPException(status_code=422, detail="Empty file")
    if file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File larger than {MAX_UPLOAD_BYTES // 2**20} MB")

    new_id = uuid4()
    key = f"{new_id}/recording{ext}"  # never the client's filename: it is untrusted
    # Object first, row second: a failed insert leaves only an orphan object, never a job without audio.
    recordings.put(settings, key, file.file, file.size, AUDIO_TYPES[ext])
    with store.connect(settings.database_url) as conn:
        store.create_queued_call(conn, principal.org_id, new_id, call_id, key)
        store.audit(conn, principal.org_id, principal.actor, "call.create", str(new_id), {"source": "audio"})
    return {"id": new_id, "status": "queued"}


@app.post("/v1/orgs/{org_id}/connectors/{name}/webhook")
async def connector_webhook(org_id: UUID, name: str, request: Request) -> dict:
    """Inbound calls from other systems, signed with the org's webhook secret
    (`python -m callsight.admin webhook-secret`). Transcripts are analyzed now; recording URLs are queued."""
    settings = Settings.from_env()
    database_url = _database_url(settings)
    load_builtins()
    if name not in available("connector"):
        raise HTTPException(status_code=404, detail=f"Unknown connector '{name}'")
    with store.connect(database_url) as conn:
        secret = store.org_webhook_secret(conn, org_id)
        competitors = store.org_competitors(conn, org_id)
    if secret is None:
        raise HTTPException(status_code=404, detail="Unknown organization")
    body = await request.body()
    try:
        inbound = create("connector", name, secret=secret).parse(body, request.headers)
    except Unauthorized as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    actor = f"webhook:{name}"
    if inbound.recording_url is not None:
        try:
            remote.check_url(inbound.recording_url)  # reject internal URLs now; the worker checks again
        except remote.UnsafeURL as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        new_id = uuid4()
        with store.connect(database_url) as conn:
            store.create_queued_call(
                conn, org_id, new_id, inbound.external_id, recording_url=inbound.recording_url, source=name
            )
            store.audit(conn, org_id, actor, "call.create", str(new_id), {"source": name, "via": "recording_url"})
        return {"id": new_id, "status": "queued"}
    try:
        transcript, report = analyze_text_with_transcript(
            inbound.transcript, call_id=inbound.external_id, settings=settings, extra_competitors=competitors
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with store.connect(database_url) as conn:
        call_id = store.save_call(conn, org_id, transcript, report, source=name)
        store.audit(conn, org_id, actor, "call.create", str(call_id), {"source": name, "via": "transcript"})
    return {"id": call_id, "status": "done"}


@app.get("/v1/calls/{call_id}/recording")
def get_recording(
    call_id: UUID, request: Request,
    org: UUID | None = None, by: str | None = None, expires: int | None = None, sig: str | None = None,
):
    """Stream the stored recording. Authorized by an API key, or by the signed link that
    GET /v1/calls/{id} returns (audio players cannot send headers). Supports Range requests."""
    settings = Settings.from_env()
    if sig is not None:
        valid = org is not None and by is not None and expires is not None and auth.verify_recording(
            call_id, org, by, expires, sig, settings.secret_key
        )
        if not valid:
            raise HTTPException(status_code=401, detail="Invalid or expired recording link")
        principal = Principal(org, f"{by} (link)")
    else:
        principal = require_key(request)
    with store.connect(_database_url(settings)) as conn:
        key = store.recording_key(conn, principal.org_id, call_id)
    if not key or not settings.s3_endpoint:
        raise HTTPException(status_code=404, detail="No recording for this call")
    total = recordings.size(settings, key)
    try:
        span = recordings.parse_range(request.headers.get("range"), total)
    except ValueError:
        raise HTTPException(status_code=416, headers={"Content-Range": f"bytes */{total}"}) from None
    start, end = span or (0, total - 1)
    if start == 0:  # players fetch many ranges while seeking; log the first one only
        with store.connect(settings.database_url) as conn:
            store.audit(conn, principal.org_id, principal.actor, "recording.read", str(call_id))
    obj = recordings.open_stream(settings, key, offset=start, length=end - start + 1)

    def chunks():
        try:
            yield from obj.stream(1 << 16)
        finally:
            obj.close()
            obj.release_conn()

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1)}
    if span:
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
    media = AUDIO_TYPES.get(PurePath(key).suffix, "application/octet-stream")
    return StreamingResponse(chunks(), status_code=206 if span else 200, media_type=media, headers=headers)


@app.get("/v1/calls")
def list_calls(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    q: str | None = Query(None, max_length=200, description="Match the call name or anything said in it"),
    status: Literal["queued", "processing", "done", "failed"] | None = None,
    principal: Principal = Depends(require_key),
) -> list[dict]:
    with store.connect(Settings.from_env().database_url) as conn:
        return store.list_calls(conn, principal.org_id, limit, offset, query=(q or "").strip() or None, status=status)


@app.get("/v1/calls/{call_id}")
def get_call(call_id: UUID, principal: Principal = Depends(require_key)) -> dict:
    settings = Settings.from_env()
    with store.connect(settings.database_url) as conn:
        call = store.get_call(conn, principal.org_id, call_id)
        if call is None:
            raise HTTPException(status_code=404, detail="Call not found")
        store.audit(conn, principal.org_id, principal.actor, "call.read", str(call_id))
    call["recording_link"] = (
        auth.sign_recording(call_id, principal.org_id, principal.actor, settings.secret_key)
        if call["has_recording"]
        else None
    )
    return call


@app.delete("/v1/calls/{call_id}", status_code=204)
def delete_call(call_id: UUID, principal: Principal = Depends(require_key)) -> None:
    """Delete a call, its transcript, insights and recording. The audit log keeps a record that it happened."""
    if not retention.delete_call(Settings.from_env(), principal.org_id, call_id, principal.actor):
        raise HTTPException(status_code=404, detail="Call not found")


SpeakerName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]


class SpeakerUpdate(BaseModel):
    names: dict[str, SpeakerName] = Field(default_factory=dict, max_length=20,
                                          description='Renames, e.g. {"Speaker 1": "Alex"}')
    rep: SpeakerName | None = Field(None, description="Which speaker (after renaming) is the sales rep")


def _reanalyze(call_id: UUID, principal: Principal, update: SpeakerUpdate | None = None) -> dict:
    """Re-run analysis on a stored call with the org's current settings, optionally renaming speakers."""
    settings = Settings.from_env()
    with store.connect(settings.database_url) as conn:
        loaded = store.load_transcript(conn, principal.org_id, call_id)
        competitors = store.org_competitors(conn, principal.org_id)
    if loaded is None:
        raise HTTPException(status_code=404, detail="Call not found")
    transcript, row = loaded
    if row["status"] != "done":
        raise HTTPException(status_code=409, detail="Only finished calls can be re-analyzed")
    rep = row["rep_speaker"]
    if update is not None:
        unknown = set(update.names) - set(transcript.speakers)
        if unknown:
            raise HTTPException(status_code=422, detail=f"Not speakers on this call: {sorted(unknown)}")
        renamed = [update.names.get(s, s) for s in transcript.speakers]
        if len(set(renamed)) != len(renamed):
            raise HTTPException(status_code=422, detail="Two speakers would end up with the same name")
        transcript = Transcript(transcript.call_id, [replace(s, speaker=update.names.get(s.speaker, s.speaker))
                                                     for s in transcript.segments])
        rep = update.names.get(rep, rep) if update.rep is None else update.rep
        if rep is not None and rep not in renamed:
            raise HTTPException(status_code=422, detail=f"The rep must be one of {renamed}")
    report = analyze_transcript(transcript, settings=settings, extra_competitors=competitors, rep_speaker=rep)
    with store.connect(settings.database_url) as conn:
        store.replace_analysis(conn, call_id, transcript, report, rep)
        action = "call.speakers" if update is not None else "call.reanalyze"
        details = {"names": update.names, "rep": rep} if update is not None else {}
        store.audit(conn, principal.org_id, principal.actor, action, str(call_id), details)
    return get_call(call_id, principal)


@app.post("/v1/calls/{call_id}/reanalyze")
def reanalyze_call(call_id: UUID, principal: Principal = Depends(require_key)) -> dict:
    """Re-run analysis with the current settings (competitor lists, rules, LLM). Returns the updated call."""
    return _reanalyze(call_id, principal)


@app.put("/v1/calls/{call_id}/speakers")
def update_speakers(call_id: UUID, body: SpeakerUpdate, principal: Principal = Depends(require_key)) -> dict:
    """Rename speakers and/or say who the rep is, then re-analyze. Returns the updated call."""
    return _reanalyze(call_id, principal, body)


class OrgSettings(BaseModel):
    competitors: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]] = Field(
        default_factory=list, max_length=50, description="Competitor names to flag when mentioned on a call"
    )


@app.get("/v1/settings")
def get_settings(principal: Principal = Depends(require_key)) -> OrgSettings:
    """This organization's settings."""
    with store.connect(Settings.from_env().database_url) as conn:
        return OrgSettings(competitors=list(store.org_competitors(conn, principal.org_id)))


@app.put("/v1/settings")
def put_settings(body: OrgSettings, principal: Principal = Depends(require_key)) -> OrgSettings:
    """Replace this organization's settings. They apply to calls analyzed from now on."""
    competitors = list(dict.fromkeys(body.competitors))  # keep order, drop duplicates
    with store.connect(Settings.from_env().database_url) as conn:
        store.set_org_competitors(conn, principal.org_id, competitors)
        store.audit(conn, principal.org_id, principal.actor, "settings.update", None, {"competitors": competitors})
    return OrgSettings(competitors=competitors)


@app.get("/v1/trends")
def get_trends(days: int = Query(30, ge=1, le=365), principal: Principal = Depends(require_key)) -> dict:
    """Patterns across this organization's finished calls in the last `days` days."""
    with store.connect(Settings.from_env().database_url) as conn:
        return store.trends(conn, principal.org_id, days)


@app.get("/v1/audit")
def audit_log(
    limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), principal: Principal = Depends(require_key)
) -> list[dict]:
    """Who created or read what in this organization, newest first."""
    with store.connect(Settings.from_env().database_url) as conn:
        return store.list_audit(conn, principal.org_id, limit, offset)
