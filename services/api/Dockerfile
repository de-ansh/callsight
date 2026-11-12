FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
# Add diarize for speaker separation in mono recordings (CPU PyTorch):
#   docker compose build --build-arg EXTRAS=asr,diarize
ARG EXTRAS=asr
# PyPI's torch bundles ~4 GB of CUDA libraries; the worker runs on CPU, so add the CPU index
# (its +cpu builds win on version). Resolved in one pass: preinstalling torch made pip backtrack.
RUN case ",${EXTRAS}," in *,diarize,*) cpu="--extra-index-url https://download.pytorch.org/whl/cpu" ;; esac; \
    pip install --no-cache-dir $cpu ".[${EXTRAS}]"

RUN useradd --create-home appuser
USER appuser
# Whisper models download here on first use; compose mounts a volume so they persist.
RUN mkdir -p /home/appuser/.cache

EXPOSE 8000
CMD ["uvicorn", "callsight.api:app", "--host", "0.0.0.0", "--port", "8000"]
