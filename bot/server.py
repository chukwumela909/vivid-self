"""FastAPI signaling server for the SmallWebRTC voice bot.

The browser's Pipecat SmallWebRTC transport speaks two methods against
/api/offer: POST carries the SDP offer, PATCH trickles ICE candidates. Both
are delegated to Pipecat's SmallWebRTCRequestHandler. Provider API keys never
leave this process; one bot pipeline is spawned per peer connection.
"""

import json
import os

import uvicorn
from fastapi import BackgroundTasks, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from pipecat.transports.smallwebrtc.connection import IceServer
from pipecat.transports.smallwebrtc.request_handler import (
    IceCandidate,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)

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

# Handles connection lifecycle, renegotiation, and ICE candidate patches.
webrtc = SmallWebRTCRequestHandler(ice_servers=ICE_SERVERS)

app = FastAPI()

# CORS: the Next.js app calls this from the browser. Tighten allow_origins to
# your web domain in production if you like.
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
    # Spawn the pipeline only for a brand-new connection (not on renegotiation).
    # add_task runs after the answer is returned, so the offer isn't blocked.
    async def on_new_connection(connection):
        background_tasks.add_task(run_bot, connection)

    return await webrtc.handle_web_request(
        SmallWebRTCRequest.from_dict(request), on_new_connection
    )


@app.patch("/api/offer")
async def offer_patch(request: dict):
    candidates = [IceCandidate(**c) for c in request.get("candidates", [])]
    await webrtc.handle_patch_request(
        SmallWebRTCPatchRequest(pc_id=request["pc_id"], candidates=candidates)
    )
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.getenv("BOT_PORT", "7860"))
    uvicorn.run(app, host="0.0.0.0", port=port)
