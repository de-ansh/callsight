"""HTTP API. Thin wrapper over callsight.service; requires fastapi (see pyproject.toml)."""

from __future__ import annotations

from uuid import UUID

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from . import __version__, store
from .config import Settings
from .service import analyze_text_with_transcript

app = FastAPI(title="Callsight", version=__version__)


class AnalyzeRequest(BaseModel):
    call_id: str = "adhoc"
    transcript: str = Field(description="Plain text, one turn per line: '[mm:ss] Speaker: text'")
    competitors: list[str] = Field(default_factory=list)


def _database_url(settings: Settings) -> str:
    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Persistence is disabled: CALLSIGHT_DATABASE_URL is not set")
    return settings.database_url


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
