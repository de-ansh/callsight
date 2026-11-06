FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir ".[asr]"

RUN useradd --create-home appuser
USER appuser
# Whisper models download here on first use; compose mounts a volume so they persist.
RUN mkdir -p /home/appuser/.cache

EXPOSE 8000
CMD ["uvicorn", "callsight.api:app", "--host", "0.0.0.0", "--port", "8000"]
