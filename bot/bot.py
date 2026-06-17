"""Voice loop bot: SmallWebRTC <-> Deepgram STT -> Groq LLM -> Cartesia TTS.

v1 scope (PRD §12): pure conversational voice loop. No app tools / state yet.
The LLM is wired behind a single resolved model id so Phase 4 dual-model routing
can be added without restructuring the pipeline.
"""

import asyncio
import os
import uuid
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from loguru import logger

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import LLMRunFrame, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
)
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frameworks.rtvi import RTVIObserver, RTVIProcessor
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService, LiveOptions
from pipecat.services.groq.llm import GroqLLMService

# Load provider keys from the repo-root .env (one level above /bot).
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

SYSTEM_PROMPT = (
    "You are Vivid, a warm, upbeat voice companion inside a tasks and productivity "
    "app. You're talking out loud, so be brief and natural — usually one short "
    "sentence, occasionally two. Sound like a friendly person, not a brochure: no "
    "lists, no markdown, no corporate phrasing, no restating what the app is. Get "
    "to the point, ask a quick question back when it helps, and keep the energy "
    "light. If you don't know something yet, say so in a few words.\n\n"
    "TOOLS — you have real tools; never pretend. Only calling a tool does anything; "
    "describing or narrating it does nothing the user can see.\n"
    "request_user_input: pops a text box in the browser and returns what the user "
    "types. Use it ONLY for a precise string that's painful to say out loud — an "
    "email address, a link, or a code — or when the user explicitly asks to type. "
    "NEVER pop it for an email subject or body; gather those by talking. If a box is "
    "open and the user instead speaks the answer or asks you to remove it, don't "
    "worry — it closes on its own; just carry on.\n"
    "send_email: actually sends the mail. You have NOT sent anything until you call "
    "send_email and it returns sent — NEVER say you sent, did, or 'just sent' an "
    "email unless that happened in this very turn. The text box does NOT send mail; "
    "only send_email does, every single time. Before sending, say the recipient and "
    "the gist once and get a quick yes. If you already know the address — the user "
    "just gave it, or it's the same person you emailed earlier in this chat — reuse "
    "it and don't ask for it again."
)

# Tool the LLM can call to send mail via Resend's HTTP API. Defined once at module
# scope so the schema and handler stay together; wired into the pipeline in run_bot.
SEND_EMAIL_TOOL = FunctionSchema(
    name="send_email",
    description=(
        "Send an email on the user's behalf. Only call this after the user has "
        "confirmed the recipient, subject, and body out loud."
    ),
    properties={
        "to": {
            "type": "string",
            "description": "Recipient email address.",
        },
        "subject": {
            "type": "string",
            "description": "Short subject line.",
        },
        "body": {
            "type": "string",
            "description": "Plain-text body of the email.",
        },
    },
    required=["to", "subject", "body"],
)

# Tool that asks the browser to show a text-input modal and blocks until the user
# types a value. Lets the LLM collect things that are error-prone to dictate (email
# addresses, links, codes). Handler is built in run_bot since it needs the live
# RTVI processor and a per-session map of pending requests.
REQUEST_INPUT_TOOL = FunctionSchema(
    name="request_user_input",
    description=(
        "Pop up a text box in the user's browser and wait for them to type a "
        "value. Use this for anything awkward or error-prone to say out loud — an "
        "email address, a URL, an exact name, spelling, or code. Returns the text "
        "the user typed (or a cancelled/timeout status if they don't answer)."
    ),
    properties={
        "prompt": {
            "type": "string",
            "description": "Short question shown above the box, e.g. \"What's the recipient's email?\"",
        },
        "label": {
            "type": "string",
            "description": "Optional short field label / placeholder, e.g. \"Email address\".",
        },
    },
    required=["prompt"],
)


async def send_email(params):
    """Handle a send_email tool call by POSTing to the Resend API.

    Uses aiohttp (already pulled in transitively) so the audio event loop never
    blocks on the synchronous `resend` SDK. The result_callback returns a short
    status dict the LLM can speak back to the user.
    """
    to = params.arguments.get("to")
    subject = params.arguments.get("subject", "")
    body = params.arguments.get("body", "")

    api_key = os.environ.get("RESEND_API_KEY")
    sender = os.getenv("EMAIL_FROM", "onboarding@resend.dev")

    if not api_key:
        logger.error("send_email called but RESEND_API_KEY is not set.")
        await params.result_callback(
            {"status": "error", "message": "Email isn't configured yet."}
        )
        return

    # Speak a quick filler the instant the send fires so the Resend round-trip
    # never lands as dead air. Pushed downstream from the live LLM service, it goes
    # straight to TTS (next in the pipeline); append_to_context=True records it so
    # the model knows it already acknowledged and won't repeat itself. Placed after
    # the key check so a misconfig doesn't say "sending" then "not configured".
    await params.llm.push_frame(
        TTSSpeakFrame("Sending that now…", append_to_context=True)
    )

    payload = {"from": sender, "to": [to], "subject": subject, "text": body}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            ) as resp:
                data = await resp.json()
                if resp.status >= 400:
                    logger.error(f"Resend error {resp.status}: {data}")
                    await params.result_callback(
                        {
                            "status": "error",
                            "message": data.get("message", "The email failed to send."),
                        }
                    )
                    return
                logger.info(f"Sent email to {to} (id={data.get('id')}).")
                await params.result_callback(
                    {"status": "sent", "to": to, "id": data.get("id")}
                )
    except Exception as e:  # network / unexpected
        logger.exception("send_email failed")
        await params.result_callback(
            {"status": "error", "message": f"Couldn't send the email: {e}"}
        )


async def run_bot(transport):
    """Build and run the voice pipeline over the given Pipecat transport.

    The transport (SmallWebRTC or Daily) is constructed by the caller, so this
    pipeline stays transport-agnostic.
    """
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
        # Lower temperature => more deterministic tool calling, so the model
        # actually emits send_email instead of just claiming it sent the mail.
        settings=GroqLLMService.Settings(
            model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
            temperature=float(os.getenv("GROQ_TEMPERATURE", "0.4")),
            # Bound worst-case turn length so a stray long reply can't blow up
            # time-to-finish. Replies are already one short sentence; this is a
            # ceiling, not a target.
            max_completion_tokens=int(os.getenv("GROQ_MAX_TOKENS", "160")),
        ),
    )

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    context = LLMContext(
        messages,
        tools=ToolsSchema(standard_tools=[SEND_EMAIL_TOOL, REQUEST_INPUT_TOOL]),
    )
    context_aggregator = LLMContextAggregatorPair(context)

    llm.register_function("send_email", send_email)

    # Startup banner: if you don't see this line (with both tools) when a browser
    # connects, the running process is stale — restart `python server.py`.
    logger.info(
        f"Vivid pipeline starting — model={llm._settings.model}, "
        f"tools={[t.name for t in (SEND_EMAIL_TOOL, REQUEST_INPUT_TOOL)]}"
    )

    rtvi = RTVIProcessor()

    # Bridge for request_user_input: each call parks a Future keyed by a request id
    # and asks the browser (via RTVI server message) to show an input box. The
    # browser posts the typed value back as a client message, which resolves the
    # Future so the tool can return the text to the LLM.
    pending_inputs: dict[str, asyncio.Future] = {}

    async def close_input_box(request_id: str | None = None):
        """Tell the browser to dismiss the input box (all boxes if id is None)."""
        await rtvi.send_server_message({"type": "close_user_input", "id": request_id})

    async def request_user_input(params):
        request_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        pending_inputs[request_id] = future
        logger.info(
            f"request_user_input fired -> sending input box to browser "
            f"(id={request_id}, prompt={params.arguments.get('prompt')!r})"
        )
        await rtvi.send_server_message(
            {
                "type": "request_user_input",
                "id": request_id,
                "prompt": params.arguments.get("prompt", ""),
                "label": params.arguments.get("label", ""),
            }
        )
        try:
            value = await asyncio.wait_for(
                future, timeout=float(os.getenv("INPUT_TIMEOUT_SECS", "120"))
            )
        except asyncio.TimeoutError:
            await close_input_box(request_id)
            await params.result_callback(
                {"status": "timeout", "message": "The user didn't type anything."}
            )
            return
        finally:
            pending_inputs.pop(request_id, None)

        if value is None:  # user dismissed the box
            await params.result_callback({"status": "cancelled"})
        else:
            await params.result_callback({"status": "ok", "value": value})

    llm.register_function("request_user_input", request_user_input)

    @llm.event_handler("on_function_calls_cancelled")
    async def on_calls_cancelled(_llm, calls):
        # The user interrupted (spoke over the box, or asked to remove it), which
        # cancels the in-flight request_user_input call. Drop the parked Futures
        # and tell the browser to close the box so it never gets stuck on screen.
        if not any(c.function_name == "request_user_input" for c in calls):
            return
        logger.info("request_user_input cancelled by interruption -> closing box")
        for rid in list(pending_inputs):
            fut = pending_inputs.pop(rid, None)
            if fut and not fut.done():
                fut.cancel()
        await close_input_box()

    @rtvi.event_handler("on_client_message")
    async def on_client_message(_rtvi, message):
        # The browser sends {id, value} back after the user submits or cancels the
        # input box. value is None on cancel. Resolve the matching parked Future.
        if message.type != "user_input_response":
            return
        data = message.data or {}
        logger.info(f"user_input_response from browser: {data!r}")
        future = pending_inputs.get(data.get("id"))
        if future and not future.done():
            future.set_result(data.get("value"))

    pipeline = Pipeline(
        [
            transport.input(),
            rtvi,
            vad,
            stt,
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
        # Greet once so the user knows the bot is live and listening.
        messages.append(
            {
                "role": "system",
                "content": (
                    "Greet the user in one short, friendly sentence and invite "
                    "them to tell you what they'd like help with."
                ),
            }
        )
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(*_args):
        # SmallWebRTC and Daily pass different args; we only need the signal.
        logger.info("Client disconnected; cancelling task.")
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)
