"""HTTP API. Thin wrapper over callsight.service; requires fastapi (see pyproject.toml)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path, PurePath
from uuid import UUID, uuid4

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import __version__, recordings, remote, store
from .recordings import AUDIO_TYPES
from .config import Settings
from .connectors import Unauthorized
from .migrate import migrate
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


class AnalyzeRequest(BaseModel):
    call_id: str = "adhoc"
    transcript: str = Field(description="Plain text, one turn per line: '[mm:ss] Speaker: text'")
    competitors: list[str] = Field(default_factory=list)


def _database_url(settings: Settings) -> str:
    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Persistence is disabled: CALLSIGHT_DATABASE_URL is not set")
    return settings.database_url


_STATIC = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
def ui() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.post("/v1/analyze")
def analyze_call(request: AnalyzeRequest) -> dict:
    """Analyze a transcript. When a database is configured the call is stored and its id returned."""
    settings = Settings.from_env()
    try:
        transcript, report = analyze_text_with_transcript(
            request.transcript, call_id=request.call_id, settings=settings, extra_competitors=request.competitors
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    result = report.to_dict()
    if settings.database_url:
        with store.connect(settings.database_url) as conn:
            result["id"] = store.save_call(conn, transcript, report)
    return result


@app.post("/v1/calls/upload", status_code=202)
def upload_call(file: UploadFile = File(...), call_id: str = Form("upload")) -> dict:
    """Store a recording and queue it for transcription and analysis by the worker."""
    settings = Settings.from_env()
    database_url = _database_url(settings)
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
    with store.connect(database_url) as conn:
        store.create_queued_call(conn, new_id, call_id, key)
    return {"id": new_id, "status": "queued"}


@app.post("/v1/connectors/{name}/webhook")
async def connector_webhook(name: str, request: Request) -> dict:
    """Inbound calls from other systems. Transcripts are analyzed now; recording URLs are queued."""
    settings = Settings.from_env()
    database_url = _database_url(settings)
    if not settings.webhook_secret:
        raise HTTPException(status_code=503, detail="Webhooks are disabled: CALLSIGHT_WEBHOOK_SECRET is not set")
    load_builtins()
    if name not in available("connector"):
        raise HTTPException(status_code=404, detail=f"Unknown connector '{name}'")
    body = await request.body()
    try:
        inbound = create("connector", name, secret=settings.webhook_secret).parse(body, request.headers)
    except Unauthorized as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if inbound.recording_url is not None:
        try:
            remote.check_url(inbound.recording_url)  # reject internal URLs now; the worker checks again
        except remote.UnsafeURL as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        new_id = uuid4()
        with store.connect(database_url) as conn:
            store.create_queued_call(conn, new_id, inbound.external_id, recording_url=inbound.recording_url, source=name)
        return {"id": new_id, "status": "queued"}
    try:
        transcript, report = analyze_text_with_transcript(inbound.transcript, call_id=inbound.external_id, settings=settings)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with store.connect(database_url) as conn:
        call_id = store.save_call(conn, transcript, report, source=name)
    return {"id": call_id, "status": "done"}


@app.get("/v1/calls/{call_id}/recording")
def get_recording(call_id: UUID, request: Request):
    """Stream the stored recording. Supports Range requests, which browsers need to seek."""
    settings = Settings.from_env()
    with store.connect(_database_url(settings)) as conn:
        key = store.recording_key(conn, call_id)
    if not key or not settings.s3_endpoint:
        raise HTTPException(status_code=404, detail="No recording for this call")
    total = recordings.size(settings, key)
    try:
        span = recordings.parse_range(request.headers.get("range"), total)
    except ValueError:
        raise HTTPException(status_code=416, headers={"Content-Range": f"bytes */{total}"}) from None
    start, end = span or (0, total - 1)
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
def list_calls(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)) -> list[dict]:
    with store.connect(_database_url(Settings.from_env())) as conn:
        return store.list_calls(conn, limit, offset)


@app.get("/v1/calls/{call_id}")
def get_call(call_id: UUID) -> dict:
    with store.connect(_database_url(Settings.from_env())) as conn:
        call = store.get_call(conn, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return call
