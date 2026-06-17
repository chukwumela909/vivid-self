"""FastAPI signaling server for the SmallWebRTC voice bot.

Exposes POST /api/offer for WebRTC SDP exchange. The browser's Pipecat
SmallWebRTC transport connects here directly; provider API keys never leave
this process. One bot pipeline is spawned per peer connection.
"""

import json
import os

import uvicorn
from fastapi import BackgroundTasks, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from pipecat.transports.smallwebrtc.connection import IceServer, SmallWebRTCConnection

from bot import run_bot


def _load_ice_servers() -> list[IceServer]:
    """Build the ICE server list from the WEBRTC_ICE_SERVERS env var.

    Format: a JSON array whose items are either a bare STUN/TURN url string,
    or an object {"urls": ..., "username": ..., "credential": ...} for TURN.
    Empty/unset -> no ICE servers (host candidates only; fine for same-LAN
    testing, but remote browsers behind NAT need at least STUN, and a bridged
    Docker container needs TURN to be reliable — see DEPLOY.md).

    Example:
      WEBRTC_ICE_SERVERS='[
        "stun:stun.l.google.com:19302",
        {"urls":"turn:turn.example.com:3478","username":"u","credential":"p"}
      ]'
    """
    raw = os.getenv("WEBRTC_ICE_SERVERS", "").strip()
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"WEBRTC_ICE_SERVERS is not valid JSON ({e}); ignoring.")
        return []

    servers: list[IceServer] = []
    for entry in entries:
        if isinstance(entry, str):
            servers.append(IceServer(urls=entry))
        elif isinstance(entry, dict) and entry.get("urls"):
            servers.append(
                IceServer(
                    urls=entry["urls"],
                    username=entry.get("username"),
                    credential=entry.get("credential"),
                )
            )
        else:
            logger.warning(f"Skipping malformed ICE server entry: {entry!r}")
    logger.info(f"Loaded {len(servers)} ICE server(s).")
    return servers


ICE_SERVERS = _load_ice_servers()

# Active connections keyed by peer-connection id (for renegotiation).
connections: dict[str, SmallWebRTCConnection] = {}

app = FastAPI()

# Dev CORS: the Next.js app (http://localhost:3000) calls this from the browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/offer")
async def offer(request: dict, background_tasks: BackgroundTasks):
    pc_id = request.get("pc_id")

    if pc_id and pc_id in connections:
        conn = connections[pc_id]
        logger.info(f"Renegotiating existing connection {pc_id}")
        await conn.renegotiate(
            sdp=request["sdp"], type=request["type"], restart_pc=request.get("restart_pc", False)
        )
    else:
        conn = SmallWebRTCConnection(ice_servers=ICE_SERVERS)
        await conn.initialize(sdp=request["sdp"], type=request["type"])

        @conn.event_handler("closed")
        async def on_closed(c: SmallWebRTCConnection):
            logger.info(f"Connection {c.pc_id} closed")
            connections.pop(c.pc_id, None)

        connections[conn.pc_id] = conn
        background_tasks.add_task(run_bot, conn)

    answer = conn.get_answer()
    return answer


if __name__ == "__main__":
    port = int(os.getenv("BOT_PORT", "7860"))
    uvicorn.run(app, host="0.0.0.0", port=port)
