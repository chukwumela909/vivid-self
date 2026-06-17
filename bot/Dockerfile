# Python voice bot: FastAPI signaling + Pipecat SmallWebRTC pipeline.
# Pinned to 3.12 — Pipecat/aiortc wheels lag on 3.13.
FROM python:3.12-slim

# Runtime libs:
#   libgomp1   -> onnxruntime (Silero VAD)
#   libopus0   -> aiortc audio codec
#   ffmpeg     -> PyAV / media handling
# build-essential is kept for the rare source-built wheel; drop it if your
# build is fully binary-wheel and you want a smaller image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libgomp1 \
        libopus0 \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    BOT_PORT=7860

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 7860

# Signaling listens on 7860 (proxied by Coolify/Traefik over HTTPS).
# WebRTC media uses UDP and is negotiated via ICE — see DEPLOY.md.
CMD ["python", "server.py"]
