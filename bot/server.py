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

from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import IceServer
from pipecat.transports.smallwebrtc.request_handler import (
    IceCandidate,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

from bot import run_bot

# Which media transport to use: "smallwebrtc" (peer-to-peer, default, good for
# local dev) or "daily" (managed WebRTC infra; needs DAILY_API_KEY, best for
# cloud where NAT/Docker would otherwise block SmallWebRTC media).
TRANSPORT = os.getenv("TRANSPORT", "smallwebrtc").lower()


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
    return {"status": "ok", "transport": TRANSPORT}


@app.post("/api/offer")
async def offer(request: dict, background_tasks: BackgroundTasks):
    # Spawn the pipeline only for a brand-new connection (not on renegotiation).
    # add_task runs after the answer is returned, so the offer isn't blocked.
    async def on_new_connection(connection):
        transport = SmallWebRTCTransport(
            webrtc_connection=connection,
            params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
        )
        background_tasks.add_task(run_bot, transport)

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


@app.post("/connect")
async def connect(background_tasks: BackgroundTasks):
    """Daily transport: create a room + tokens, spawn the bot, return client creds.

    Daily imports are deferred so SmallWebRTC-only setups (e.g. local Windows,
    where daily-python has no wheels) never need the Daily dependency installed.
    """
    import time

    import aiohttp
    from pipecat.transports.daily.transport import DailyParams, DailyTransport
    from pipecat.transports.daily.utils import (
        DailyRESTHelper,
        DailyRoomParams,
        DailyRoomProperties,
    )

    api_key = os.environ["DAILY_API_KEY"]
    # Short-lived room that auto-ejects when it expires (1 hour).
    async with aiohttp.ClientSession() as session:
        helper = DailyRESTHelper(daily_api_key=api_key, aiohttp_session=session)
        room = await helper.create_room(
            DailyRoomParams(
                properties=DailyRoomProperties(
                    exp=time.time() + 3600, eject_at_room_exp=True
                )
            )
        )
        bot_token = await helper.get_token(room.url, 3600)
        client_token = await helper.get_token(room.url, 3600)

    transport = DailyTransport(
        room.url,
        bot_token,
        "Vivid",
        DailyParams(audio_in_enabled=True, audio_out_enabled=True),
    )
    background_tasks.add_task(run_bot, transport)
    # The browser joins the same room with its own token.
    return {"room_url": room.url, "token": client_token}


if __name__ == "__main__":
    port = int(os.getenv("BOT_PORT", "7860"))
    uvicorn.run(app, host="0.0.0.0", port=port)
