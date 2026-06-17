"""Voice loop bot: SmallWebRTC <-> Deepgram STT -> Groq LLM -> Cartesia TTS.

v1 scope (PRD §12): pure conversational voice loop. No app tools / state yet.
The LLM is wired behind a single resolved model id so Phase 4 dual-model routing
can be added without restructuring the pipeline.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
)
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.filters.wake_check_filter import WakeCheckFilter
from pipecat.processors.frameworks.rtvi import RTVIObserver, RTVIProcessor
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService, LiveOptions
from pipecat.services.groq.llm import GroqLLMService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

# Load provider keys from the repo-root .env (one level above /bot).
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

SYSTEM_PROMPT = (
    "You are Vivid, a warm, upbeat voice companion inside a tasks and productivity "
    "app. You're talking out loud, so be brief and natural — usually one short "
    "sentence, occasionally two. Sound like a friendly person, not a brochure: no "
    "lists, no markdown, no corporate phrasing, no restating what the app is. Get "
    "to the point, ask a quick question back when it helps, and keep the energy "
    "light. If you don't know something yet, say so in a few words."
)


async def run_bot(webrtc_connection):
    """Build and run the voice pipeline for a single SmallWebRTC connection."""
    transport = SmallWebRTCTransport(
        webrtc_connection=webrtc_connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
    )

    # VAD drives turn-taking and interruption. Lower stop_secs => the bot decides
    # the user is done sooner (snappier), at some risk of clipping long pauses.
    vad = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            params=VADParams(
                stop_secs=float(os.getenv("VAD_STOP_SECS", "0.5")),
                start_secs=float(os.getenv("VAD_START_SECS", "0.2")),
            )
        )
    )

    stt = DeepgramSTTService(
        api_key=os.environ["DEEPGRAM_API_KEY"],
        live_options=LiveOptions(
            model=os.getenv("DEEPGRAM_MODEL", "nova-3"),
            language="en-US",
            interim_results=True,
            smart_format=True,
            punctuate=True,
            # Faster finalization after speech stops (ms).
            endpointing=int(os.getenv("DEEPGRAM_ENDPOINTING", "250")),
        ),
    )
    tts = CartesiaTTSService(
        api_key=os.environ["CARTESIA_API_KEY"],
        voice_id=os.environ["CARTESIA_VOICE_ID"],
    )
    llm = GroqLLMService(
        api_key=os.environ["GROQ_API_KEY"],
        model=os.getenv("GROQ_MODEL", "llama-3.1-8b-instant"),
    )

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    context = LLMContext(messages)
    context_aggregator = LLMContextAggregatorPair(context)

    rtvi = RTVIProcessor()

    # Wake word: the agent ignores all speech until it hears "hey vivid", then
    # stays awake (passing turns to the LLM) until WAKE_KEEPALIVE seconds of no
    # speech, after which it sleeps again. No external service / key required.
    wake = WakeCheckFilter(
        wake_phrases=["hey vivid", "hey, vivid", "hey vivid."],
        keepalive_timeout=float(os.getenv("WAKE_KEEPALIVE", "30")),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            rtvi,
            vad,
            stt,
            wake,  # gate: only pass user turns once "hey vivid" was heard
            context_aggregator.user(),
            llm,
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(allow_interruptions=True, enable_metrics=True),
        observers=[RTVIObserver(rtvi)],
    )

    @rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        await rtvi.set_bot_ready()
        # Greet once and tell the user the wake word. This LLMRunFrame is queued
        # directly to the task, so it bypasses the wake gate (which only filters
        # user transcriptions).
        messages.append(
            {
                "role": "system",
                "content": (
                    "Greet the user in one short, friendly sentence and tell them "
                    "to say 'Hey Vivid' whenever they want your help."
                ),
            }
        )
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_transport, _client):
        logger.info("Client disconnected; cancelling task.")
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)
