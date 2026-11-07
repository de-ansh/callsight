"""HTTP API. Thin wrapper over callsight.service; requires fastapi (see pyproject.toml).

Every /v1 route that touches stored data needs an API key (Authorization: Bearer cs_..., or
X-API-Key), created with `python -m callsight.admin create-key`. A key belongs to one
organization and only ever sees that organization's calls. Webhooks authenticate with their
org's HMAC secret instead. Without a database, POST /v1/analyze is a stateless, open endpoint.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path, PurePath
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import __version__, auth, recordings, remote, retention, store
from .auth import Principal
from .config import Settings
from .connectors import Unauthorized
from .migrate import migrate
from .recordings import AUDIO_TYPES
from .registry import available, create, load_builtins
from .service import analyze_text_with_transcript


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = Settings.from_env()
    if settings.database_url:
        migrate(settings.database_url)
    yield


app = FastAPI(title="Callsight", version=__version__, lifespan=lifespan)

# shortcut: checked after the body is received (spooled to disk); set a matching body limit in a
# reverse proxy before exposing the API publicly.
MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # about 8 hours of 128 kbps mp3
_STATIC = Path(__file__).parent / "static"


class AnalyzeRequest(BaseModel):
    call_id: str = "adhoc"
    transcript: str = Field(description="Plain text, one turn per line: '[mm:ss] Speaker: text'")
    competitors: list[str] = Field(default_factory=list)


def _database_url(settings: Settings) -> str:
    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Persistence is disabled: CALLSIGHT_DATABASE_URL is not set")
    return settings.database_url


def require_key(request: Request) -> Principal:
    """The organization behind the request's API key; 401 without a valid one."""
    header = request.headers.get("authorization", "")
    key = header[7:].strip() if header.lower().startswith("bearer ") else request.headers.get("x-api-key")
    if not key:
        raise HTTPException(status_code=401, detail="Missing API key", headers={"WWW-Authenticate": "Bearer"})
    with store.connect(_database_url(Settings.from_env())) as conn:
        principal = auth.principal_for_key(conn, key)
    if principal is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key", headers={"WWW-Authenticate": "Bearer"})
    return principal


@app.get("/", include_in_schema=False)
def ui() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.post("/v1/analyze")
def analyze_call(request: AnalyzeRequest, http: Request) -> dict:
    """Analyze a transcript. With a database configured, an API key is required and the call is stored."""
    settings = Settings.from_env()
    principal = require_key(http) if settings.database_url else None
    try:
        transcript, report = analyze_text_with_transcript(
            request.transcript, call_id=request.call_id, settings=settings, extra_competitors=request.competitors
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
            inbound.transcript, call_id=inbound.external_id, settings=settings
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
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), principal: Principal = Depends(require_key)
) -> list[dict]:
    with store.connect(Settings.from_env().database_url) as conn:
        return store.list_calls(conn, principal.org_id, limit, offset)


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


@app.get("/v1/audit")
def audit_log(
    limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), principal: Principal = Depends(require_key)
) -> list[dict]:
    """Who created or read what in this organization, newest first."""
    with store.connect(Settings.from_env().database_url) as conn:
        return store.list_audit(conn, principal.org_id, limit, offset)
