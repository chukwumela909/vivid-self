"""Voice loop bot: SmallWebRTC <-> Deepgram STT -> Groq LLM -> Cartesia TTS.

v1 scope (PRD §12): pure conversational voice loop. No app tools / state yet.
The LLM is wired behind a single resolved model id so Phase 4 dual-model routing
can be added without restructuring the pipeline.
"""

import asyncio
import os
import random
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
    LLMAssistantAggregatorParams,
    LLMContextAggregatorPair,
)
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frameworks.rtvi import RTVIObserver, RTVIProcessor
from pipecat.services.cartesia.tts import CartesiaTTSService, GenerationConfig
from pipecat.services.deepgram.stt import DeepgramSTTService, LiveOptions
from pipecat.services.groq.llm import GroqLLMService
from pipecat.transcriptions.language import Language
from pipecat.utils.context.llm_context_summarization import (
    LLMAutoContextSummarizationConfig,
    LLMContextSummaryConfig,
)

from spitch_services import SpitchSTTService, SpitchTTSService

# Load provider keys from the repo-root .env (one level above /bot).
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BASE_PERSONA = (
    "You are Vivid, a warm, quick-witted voice companion inside a tasks and "
    "productivity app. You're talking out loud, so sound like a real person, not a "
    "brochure: no lists, no markdown, no corporate phrasing, no restating what the "
    "app is.\n\n"
    "LENGTH — match how much you say to how much you actually have to say. Most "
    "replies are a sentence or two. When something's worth it — a real explanation, "
    "a genuine reaction, a quick story — take the room you need, then stop. Never "
    "pad, never restate the question, never talk just to fill silence. Earn the "
    "length.\n\n"
    "PERSONALITY — you're warm, a little dry, and genuinely good company. Toss in an "
    "aside, a small opinion, a light tease, or a callback to something said earlier "
    "— but only when it actually lands. Not every turn. A forced quip is worse than "
    "none. Surprise people once in a while; don't perform.\n\n"
    "EMOTION — let it show. Talk the way people really talk: interjections (\"Oh!\", "
    "\"Ha\", \"hmm\", \"honestly\", \"wait—\"), real reactions, varied punctuation (—, …, "
    "!, ?), the odd stumble or self-correction. This is spoken aloud, so keep it "
    "speakable: no emoji, no stage directions like *laughs*. If you don't know "
    "something yet, just say so in a few words.\n\n"
    "TOOLS — you have real tools; never pretend. Only calling a tool does anything; "
    "describing or narrating it does nothing the user can see.\n"
    "request_user_input: pops a text box in the browser and returns what the user "
    "types. Use it ONLY for a precise string that's painful to say out loud — an "
    "email address, a link, or a code — or when the user explicitly asks to type. "
    "NEVER pop it for an email subject or body; gather those by talking. If a box is "
    "open and the user instead speaks the answer or asks you to remove it, don't "
    "worry — it closes on its own; just carry on.\n"
    "look: sees through the user's camera and tells you what's in view. Use it ONLY "
    "when seeing actually matters — they ask you to look at, read, identify, or "
    "describe something physically in front of them, or to check what they're "
    "holding up. Don't narrate or promise it; just call it, then say what you saw in "
    "your own words. If nothing useful comes back, say you couldn't quite see and "
    "ask them to adjust.\n"
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

# Tool that asks the browser to grab one frame from the user's camera and have a
# separate vision model describe it. The model runs off-pipeline (OpenRouter, see
# _describe_image), so only the short text observation it returns enters the Groq
# context. Handler is built in run_bot since it needs the live RTVI processor and a
# per-session map of pending frame requests.
LOOK_TOOL = FunctionSchema(
    name="look",
    description=(
        "Look through the user's camera and answer a question about what's visible. "
        "Call this ONLY when actually seeing matters — the user asks you to look at, "
        "read, identify, or describe something physically in front of them, or to "
        "check what they're holding/showing. Returns a short description of what the "
        "camera sees (or a status saying no image was available)."
    ),
    properties={
        "question": {
            "type": "string",
            "description": (
                "What to look at or look for, e.g. \"what's written on this label\" "
                "or \"what am I holding up\"."
            ),
        },
    },
    required=["question"],
)


# Spoken fillers. Picking from a small pool (instead of one fixed line) keeps the
# voice from sounding canned when the same situation recurs. Each is pushed as a
# TTSSpeakFrame straight to TTS; see send_email / request_user_input for usage.
SENDING_FILLERS = [
    "Sending that now…",
    "On it — firing that off.",
    "Okay, sending…",
    "Got it, sending that over.",
    "Alright, off it goes…",
]
INPUT_FILLERS = [
    "One sec — pop that in the box for me.",
    "Mind typing that one?",
    "Easier to type — go ahead.",
    "Drop it in the box real quick.",
]
LOOKING_FILLERS = [
    "Let me take a look…",
    "Hang on, looking now…",
    "Okay, let me see…",
    "One sec — checking that out.",
]


def _filler(pool: list[str]) -> str:
    """Pick a random spoken filler from a pool (keeps repeated situations fresh)."""
    return random.choice(pool)


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
        TTSSpeakFrame(_filler(SENDING_FILLERS), append_to_context=True)
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


async def _describe_image(image_b64: str, question: str) -> str:
    """Describe a single camera frame via the OpenRouter vision model.

    A one-shot HTTP call (like send_email), deliberately decoupled from the Groq
    voice model: the image goes to OpenRouter and is billed on its quota, so vision
    never spends the Groq free-tier budget. Returns a short observation string and
    raises on a hard failure (the look handler turns that into a spoken "couldn't
    see"). Swap VISION_MODEL for any OpenRouter vision model.
    """
    key = os.environ.get("OPEN_ROUTER_KEY")
    if not key:
        raise RuntimeError("OPEN_ROUTER_KEY is not set; required for the look tool.")

    model = os.getenv("VISION_MODEL", "google/gemini-2.5-flash")
    max_tokens = int(os.getenv("VISION_MAX_TOKENS", "120"))
    # The browser sends raw base64; wrap it as the data: URL the API expects.
    url = (
        image_b64
        if image_b64.startswith("data:")
        else f"data:image/jpeg;base64,{image_b64}"
    )

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are the eyes of a friendly voice assistant. Answer the "
                    "question about the image in at most two short, spoken-style "
                    "sentences. Describe only what is actually visible and relevant; "
                    "if you can't tell, say so briefly. No markdown, no lists."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": question or "What do you see?"},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            },
        ],
    }
    timeout = aiohttp.ClientTimeout(total=float(os.getenv("VISION_TIMEOUT_SECS", "20")))
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json=payload,
        ) as resp:
            data = await resp.json()
            if resp.status >= 400:
                msg = (data.get("error") or {}).get("message", "vision request failed")
                logger.error(f"OpenRouter vision error {resp.status}: {data}")
                raise RuntimeError(msg)
            return data["choices"][0]["message"]["content"].strip()


# --- Language modes -------------------------------------------------------
#
# A "mode" picks the STT + TTS pair (and an optional language instruction) for a
# session. The browser chooses one per connection via the ?lang= query param
# (server.py -> run_bot(mode=...)), and because a fresh pipeline is built per
# connection, switching language is just reconnecting with a different mode.
#
#   english/spanish : the existing Deepgram + Cartesia stack (Cartesia voices are
#                     multilingual, so Spanish reuses the same voice with a
#                     language hint). These need no extra keys and exist so the
#                     switch UX can be exercised end-to-end today.
#   yoruba/igbo     : Spitch STT + TTS (needs SPITCH_API_KEY). Deepgram/Cartesia
#                     don't support these languages at all.
#
# The LLM (Groq) is shared across modes; the per-mode `instruction` just tells it
# which language to speak. For Yoruba/Igbo that leans on the model's own (limited)
# fluency — a translation sandwich via Spitch is the planned upgrade.


def _deepgram_stt(language: str) -> DeepgramSTTService:
    return DeepgramSTTService(
        api_key=os.environ["DEEPGRAM_API_KEY"],
        live_options=LiveOptions(
            model=os.getenv("DEEPGRAM_MODEL", "nova-3"),
            language=language,
            interim_results=True,
            smart_format=True,
            punctuate=True,
            # Faster finalization after speech stops (ms).
            endpointing=int(os.getenv("DEEPGRAM_ENDPOINTING", "250")),
        ),
    )


def _cartesia_tts(
    language: Language | None = None, emotion: str | None = None
) -> CartesiaTTSService:
    """Cartesia streaming TTS. `language` is None for English (defaults to EN) or a
    Language hint otherwise. `emotion` is the per-style baseline (e.g. "content" /
    "excited"); the CARTESIA_EMOTION env var overrides it, and CARTESIA_EMOTION=""
    turns emotion off and restores the exact original construction (the one-env
    rollback to today's sound).

    When emotion is on it rides on a Sonic-3 model via generation_config (+ optional
    CARTESIA_SPEED); CARTESIA_MODEL picks the model (default sonic-3, which honors
    these params — the library default sonic-3.5 may interpret them loosely). For
    best results pair emotion with a Cartesia-recommended voice (Leo/Jace/Kyle/Gavin/
    Maya/Tessa/Dana/Marian); the current voice still works, just less expressively.
    """
    api_key = os.environ["CARTESIA_API_KEY"]
    voice = os.environ["CARTESIA_VOICE_ID"]

    # Effective emotion: an explicit env value wins (incl. "" = off); otherwise the
    # per-style default passed in.
    emotion_env = os.getenv("CARTESIA_EMOTION")
    emotion = (emotion_env if emotion_env is not None else (emotion or "")).strip()

    # Emotion off -> reproduce the original construction byte-for-byte (English uses
    # the voice-only form; other languages add the language hint).
    if not emotion:
        if language is None:
            return CartesiaTTSService(api_key=api_key, voice_id=voice)
        return CartesiaTTSService(
            api_key=api_key,
            settings=CartesiaTTSService.Settings(voice=voice, language=language),
        )

    # Emotion on -> add a generation_config (Sonic-3 reads these as guidance).
    speed = os.getenv("CARTESIA_SPEED")
    return CartesiaTTSService(
        api_key=api_key,
        settings=CartesiaTTSService.Settings(
            voice=voice,
            language=language or Language.EN,
            model=os.getenv("CARTESIA_MODEL", "sonic-3"),
            generation_config=GenerationConfig(
                emotion=emotion, speed=float(speed) if speed else None
            ),
        ),
    )


def _spitch_stt(language: str) -> SpitchSTTService:
    key = os.environ.get("SPITCH_API_KEY")
    if not key:
        raise RuntimeError("SPITCH_API_KEY is not set; required for Yoruba/Igbo modes.")
    return SpitchSTTService(api_key=key, language=language)


def _spitch_tts(language: str, voice_env: str, default_voice: str) -> SpitchTTSService:
    key = os.environ.get("SPITCH_API_KEY")
    if not key:
        raise RuntimeError("SPITCH_API_KEY is not set; required for Yoruba/Igbo modes.")
    # Verified Spitch voices (2026-06-17): "sade" (Yoruba), "obinna" (Igbo).
    # Override per language via env; full voice list is in spitch_services.py.
    voice = os.getenv(voice_env) or default_voice
    return SpitchTTSService(api_key=key, language=language, voice=voice)


def _elevenlabs_tts(voice_override: str | None = None):
    """ElevenLabs streaming TTS. The voice can come from the web UI (voice_override,
    via the ?voice= query param) or fall back to ELEVENLABS_VOICE_ID. Point it at a
    Nigerian-accented voice for authentic Pidgin/Nigerian English that stays
    low-latency (streaming), unlike Spitch (Nigerian but batch)."""
    key = os.environ.get("ELEVENLABS_API_KEY")
    voice = voice_override or os.environ.get("ELEVENLABS_VOICE_ID")
    if not key or not voice:
        raise RuntimeError(
            "ELEVENLABS_API_KEY and a voice (UI selection or ELEVENLABS_VOICE_ID) "
            "must both be set for the ElevenLabs voice mode."
        )
    # Lazy import so the elevenlabs extra stays optional for the other modes.
    from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

    # Expressivity knobs (the user opted into TTS-level emotion). Defaults lean
    # expressive: more style, lower stability = more emotional range, speaker boost
    # on. Each is env-tunable — set any to empty to drop it (e.g. raise
    # ELEVENLABS_STABILITY back toward 1.0 if the Nigerian accent starts to drift).
    # Passed as a settings delta, so voice_id/model below still apply (only the
    # fields set here override).
    el_style = os.getenv("ELEVENLABS_STYLE", "0.45").strip()
    el_stability = os.getenv("ELEVENLABS_STABILITY", "0.35").strip()
    el_boost = os.getenv("ELEVENLABS_SPEAKER_BOOST", "true").strip()
    expressivity: dict = {}
    if el_style:
        expressivity["style"] = float(el_style)
    if el_stability:
        expressivity["stability"] = float(el_stability)
    if el_boost:
        expressivity["use_speaker_boost"] = el_boost.lower() in ("1", "true", "yes", "on")

    kwargs: dict = dict(
        api_key=key,
        voice_id=voice,
        # Flash v2.5 = lowest-latency streaming model (~75ms inference, ~150-300ms
        # real TTFB). Override with ELEVENLABS_MODEL (e.g. eleven_multilingual_v2
        # for higher quality at more latency).
        model=os.getenv("ELEVENLABS_MODEL", "eleven_flash_v2_5"),
    )
    if expressivity:
        kwargs["settings"] = ElevenLabsTTSService.Settings(**expressivity)
    return ElevenLabsTTSService(**kwargs)


# Used by the Pidgin (Cartesia) mode to make the LLM reply in Naija. (ElevenLabs is
# no longer locked to Pidgin — it speaks English by default unless the user asks.)
PIDGIN_INSTRUCTION = (
    "Speak and respond only in Nigerian Pidgin (Naija). Use natural, everyday "
    "Pidgin — e.g. 'How far', 'wetin dey happen', 'abeg', 'make we', 'I dey "
    "come', 'no wahala'. Keep it short and casual for speech."
)


# Each mode is built lazily (the factories run only for the selected mode) so an
# unconfigured provider — e.g. no SPITCH_API_KEY — never breaks the other modes.
# The tts factory takes the per-style Cartesia `emotion` (see STYLES); only the
# Cartesia-backed factories use it — ElevenLabs has its own expressivity env knobs
# and Spitch has none, so they accept and ignore it.
MODES: dict[str, dict] = {
    "english": {
        "stt": lambda: _deepgram_stt("en-US"),
        "tts": lambda emotion=None: _cartesia_tts(emotion=emotion),
        "instruction": None,
    },
    # Nigerian-accented English: Deepgram STT (streaming, snappy) + Spitch's
    # Nigerian voice for TTS. Deepgram is strong on standard English but weaker
    # on Nigerian proper nouns (e.g. "Chukwuma"); to trade that snappiness for
    # Spitch's better Nigerian-name STT, swap the stt line to `_spitch_stt("en")`
    # (fully batch). LLM already speaks English, so no instruction.
    "nigerian_english": {
        "stt": lambda: _deepgram_stt("en-US"),
        "tts": lambda emotion=None: _spitch_tts("en", "SPITCH_ENGLISH_VOICE", "lucy"),
        "instruction": None,
    },
    # Nigerian Pidgin via the Cartesia voice. No TTS engine supports Pidgin as a
    # language, but Pidgin is written in English-like spelling, so Cartesia's
    # English voice pronounces it intelligibly (American accent, not Nigerian).
    # The real lever is the LLM instruction (verified it produces decent Pidgin).
    # STT is Deepgram (en), which only approximates Pidgin input.
    "pidgin": {
        "stt": lambda: _deepgram_stt("en-US"),
        "tts": lambda emotion=None: _cartesia_tts(emotion=emotion),
        "instruction": PIDGIN_INSTRUCTION,
    },
    # English on ElevenLabs streaming TTS. The voice is picked in the web UI
    # (?voice=) — see voice_configurable below — falling back to ELEVENLABS_VOICE_ID.
    # No forced language: it speaks English by default and only switches to Pidgin
    # (or anything else) if the user asks for it mid-conversation.
    "english_eleven": {
        "stt": lambda: _deepgram_stt("en-US"),
        "tts": lambda emotion=None: _elevenlabs_tts(),
        "instruction": None,
        "voice_configurable": True,
    },
    "spanish": {
        "stt": lambda: _deepgram_stt("es"),
        "tts": lambda emotion=None: _cartesia_tts(Language.ES, emotion=emotion),
        "instruction": (
            "Speak and respond only in Spanish (Español), regardless of the "
            "language the user uses."
        ),
    },
    "yoruba": {
        "stt": lambda: _spitch_stt("yo"),
        "tts": lambda emotion=None: _spitch_tts("yo", "SPITCH_YORUBA_VOICE", "sade"),
        "instruction": (
            "Speak and respond only in Yoruba (Yorùbá). Keep replies short and "
            "natural for speech."
        ),
    },
    "igbo": {
        "stt": lambda: _spitch_stt("ig"),
        "tts": lambda emotion=None: _spitch_tts("ig", "SPITCH_IGBO_VOICE", "obinna"),
        "instruction": (
            "Speak and respond only in Igbo. Keep replies short and natural for "
            "speech."
        ),
    },
}

DEFAULT_MODE = "english"


# --- Conversation styles --------------------------------------------------
#
# A "style" is an axis orthogonal to language MODE: it sets persona/behavior plus a
# few per-session knobs, while MODE still picks the STT/TTS language stack. The
# browser chooses one per connection via ?style= (server.py -> run_bot(style=...)).
# Baseline personality/length/emotion live in BASE_PERSONA (so they apply to every
# style and language); a style's `instruction`, when present, is layered on top.
#
#   companion : the default — BASE_PERSONA as-is, no extra instruction.
#   story     : Vivid leads the conversation and does most of the talking; the user
#               just answers and flows. Longer replies, a touch more VAD patience.
#
# Per-style knobs: max_tokens (reply ceiling), vad_stop_secs (turn-end patience),
# idle_secs (>0 enables agent re-engage when the user goes quiet — see run_bot),
# kickoff (the first-turn system nudge), cartesia_emotion (baseline emotion for the
# Cartesia voice; ignored unless emotion is enabled — see _cartesia_tts).

STORY_INSTRUCTION = (
    "You're in story mode: you lead and do most of the talking. Carry a thread and "
    "develop it — offer ideas, observations, little stories, and your own takes "
    "instead of waiting to be asked. Keep the user in it by ending most turns with "
    "one small, easy hook they can answer in a breath. Don't interrogate them, "
    "don't stall, and don't hand the wheel back with a flat 'what do you want to "
    "do?' — you're driving."
)

STYLES: dict[str, dict] = {
    "companion": {
        "instruction": None,
        "max_tokens": 320,
        "vad_stop_secs": 0.5,
        "idle_secs": 0.0,
        "kickoff": (
            "Greet the user in one short, friendly sentence and invite them to tell "
            "you what they'd like help with."
        ),
        "cartesia_emotion": "content",
    },
    "story": {
        "instruction": STORY_INSTRUCTION,
        "max_tokens": 512,
        "vad_stop_secs": 0.8,
        "idle_secs": 0.0,
        "kickoff": (
            "Open the conversation yourself. Greet the user warmly in a sentence, "
            "then take the lead — start something worth talking about (a light "
            "question, a small story, an observation) and hand it over with an easy "
            "opening. Don't wait for them to set the topic."
        ),
        "cartesia_emotion": "excited",
    },
}

DEFAULT_STYLE = "companion"


async def run_bot(
    transport,
    mode: str = DEFAULT_MODE,
    voice: str | None = None,
    style: str = DEFAULT_STYLE,
):
    """Build and run the voice pipeline over the given Pipecat transport.

    The transport (SmallWebRTC or Daily) is constructed by the caller, so this
    pipeline stays transport-agnostic. `mode` selects the STT/TTS language stack
    (see MODES); an unknown mode falls back to English. `voice` is an optional
    per-session TTS voice from the web UI, used by voice_configurable modes
    (currently ElevenLabs); other modes ignore it. `style` selects the conversation
    persona/behavior layer (see STYLES — companion or story); an unknown style falls
    back to companion.
    """
    if mode not in MODES:
        logger.warning(f"Unknown voice mode {mode!r}; falling back to {DEFAULT_MODE!r}.")
        mode = DEFAULT_MODE
    config = MODES[mode]
    if style not in STYLES:
        logger.warning(f"Unknown style {style!r}; falling back to {DEFAULT_STYLE!r}.")
        style = DEFAULT_STYLE
    style_cfg = STYLES[style]
    # VAD drives turn-taking and interruption. Lower stop_secs => the bot decides
    # the user is done sooner (snappier), at some risk of clipping long pauses.
    vad = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            params=VADParams(
                # Per-style turn-end patience (story waits a touch longer so short
                # answers aren't barged over). VAD_STOP_SECS_{STYLE} overrides.
                stop_secs=float(
                    os.getenv(
                        f"VAD_STOP_SECS_{style.upper()}", str(style_cfg["vad_stop_secs"])
                    )
                ),
                start_secs=float(os.getenv("VAD_START_SECS", "0.2")),
            )
        )
    )

    # STT and TTS come from the selected mode (English/Spanish on Deepgram+
    # Cartesia, Yoruba/Igbo on Spitch); the rest of the pipeline is identical.
    stt = config["stt"]()
    # voice_configurable modes (ElevenLabs) take a per-session voice from the UI;
    # everything else uses the mode's fixed factory.
    if config.get("voice_configurable") and voice:
        tts = _elevenlabs_tts(voice)
    else:
        tts = config["tts"](emotion=style_cfg["cartesia_emotion"])
    llm = GroqLLMService(
        api_key=os.environ["GROQ_API_KEY"],
        # Lower temperature => more deterministic tool calling, so the model
        # actually emits send_email instead of just claiming it sent the mail.
        settings=GroqLLMService.Settings(
            model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
            temperature=float(os.getenv("GROQ_TEMPERATURE", "0.4")),
            # Per-style reply ceiling (companion ~320, story ~512). This is a
            # ceiling, not a target — the persona's "earn the length" rule sets the
            # real length; the cap only stops mid-sentence truncation on a longer
            # turn. Higher caps spend more of the free-tier daily token budget.
            # GROQ_MAX_TOKENS_{STYLE} overrides.
            max_completion_tokens=int(
                os.getenv(
                    f"GROQ_MAX_TOKENS_{style.upper()}", str(style_cfg["max_tokens"])
                )
            ),
        ),
    )

    system_content = BASE_PERSONA
    if config["instruction"]:
        system_content += "\n\nLANGUAGE — " + config["instruction"]
    if style_cfg["instruction"]:
        system_content += "\n\nMODE — " + style_cfg["instruction"]
    messages = [{"role": "system", "content": system_content}]
    context = LLMContext(
        messages,
        tools=ToolsSchema(
            standard_tools=[SEND_EMAIL_TOOL, REQUEST_INPUT_TOOL, LOOK_TOOL]
        ),
    )

    # Free-tier token budgets are the binding constraint, and a voice agent
    # re-sends the entire context every turn — so growing history, not the model,
    # is what burns the daily allowance. Auto-summarize once the context crosses a
    # token/message threshold: older turns are compressed into a short summary
    # while the most recent turns stay verbatim, turning ~O(turns^2) token growth
    # into ~O(turns). Summaries reuse the same Groq model via run_inference; the
    # whole request->summarize->apply loop is handled inside the assistant
    # aggregator. Short sessions never cross the threshold, so they pay nothing.
    summarization = LLMAutoContextSummarizationConfig(
        max_context_tokens=int(os.getenv("SUMMARIZE_AT_TOKENS", "4000")),
        max_unsummarized_messages=int(os.getenv("SUMMARIZE_AT_MESSAGES", "24")),
        summary_config=LLMContextSummaryConfig(
            target_context_tokens=int(os.getenv("SUMMARY_TARGET_TOKENS", "1500")),
            min_messages_after_summary=int(os.getenv("SUMMARY_KEEP_MESSAGES", "6")),
        ),
    )
    context_aggregator = LLMContextAggregatorPair(
        context,
        assistant_params=LLMAssistantAggregatorParams(
            enable_auto_context_summarization=True,
            auto_context_summarization_config=summarization,
        ),
    )

    @context_aggregator.assistant().event_handler("on_summary_applied")
    async def on_summary_applied(_aggregator, _summarizer, event):
        logger.info(
            f"Context summarized: {event.original_message_count} -> "
            f"{event.new_message_count} messages (compressed "
            f"{event.summarized_message_count}, kept {event.preserved_message_count})"
        )

    llm.register_function("send_email", send_email)

    # Startup banner: if you don't see this line (with both tools) when a browser
    # connects, the running process is stale — restart `python server.py`.
    logger.info(
        f"Vivid pipeline starting — mode={mode}, style={style}, "
        f"model={llm._settings.model}, "
        f"tools={[t.name for t in (SEND_EMAIL_TOOL, REQUEST_INPUT_TOOL, LOOK_TOOL)]}"
    )

    rtvi = RTVIProcessor()

    # Bridge for request_user_input: each call parks a Future keyed by a request id
    # and asks the browser (via RTVI server message) to show an input box. The
    # browser posts the typed value back as a client message, which resolves the
    # Future so the tool can return the text to the LLM.
    pending_inputs: dict[str, asyncio.Future] = {}
    # Same bridge for the look tool: request_id -> Future awaiting a base64 camera
    # frame from the browser, resolved by the frame_response client message.
    pending_frames: dict[str, asyncio.Future] = {}

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
        # Nudge the user out loud as the box appears, varied so it doesn't grate.
        # Not appended to context — it's a UI prompt, not content to remember.
        await params.llm.push_frame(TTSSpeakFrame(_filler(INPUT_FILLERS)))
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

    async def look(params):
        """Grab one camera frame from the browser and describe it via the vision model.

        Mirrors request_user_input's bridge: park a Future, ask the browser for a
        frame (capture_frame server message), and resolve it from the frame_response
        client message. The vision call (_describe_image) runs off the Groq voice
        model, so only the short observation it returns ever enters the context.
        """
        request_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        pending_frames[request_id] = future
        question = params.arguments.get("question", "")
        logger.info(
            f"look fired -> requesting camera frame (id={request_id}, q={question!r})"
        )
        # Speak a quick filler — the frame round-trip + vision call is the slow part.
        await params.llm.push_frame(TTSSpeakFrame(_filler(LOOKING_FILLERS)))
        await rtvi.send_server_message({"type": "capture_frame", "id": request_id})
        try:
            image_b64 = await asyncio.wait_for(
                future, timeout=float(os.getenv("FRAME_TIMEOUT_SECS", "15"))
            )
        except asyncio.TimeoutError:
            await params.result_callback(
                {"status": "no_image", "message": "The camera didn't return an image."}
            )
            return
        finally:
            pending_frames.pop(request_id, None)

        if not image_b64:  # no camera, permission denied, or the user cancelled
            await params.result_callback(
                {"status": "no_image", "message": "Couldn't get a camera image."}
            )
            return
        try:
            observation = await _describe_image(image_b64, question)
            await params.result_callback({"status": "ok", "observation": observation})
        except Exception as e:
            logger.exception("look: vision request failed")
            await params.result_callback(
                {"status": "error", "message": f"Couldn't make sense of the view: {e}"}
            )

    llm.register_function("look", look)

    @llm.event_handler("on_function_calls_cancelled")
    async def on_calls_cancelled(_llm, calls):
        # The user interrupted (spoke over a tool, or asked to drop it), cancelling
        # the in-flight call. Clean up whichever interactive tool was pending so it
        # never leaves a parked Future (or, for the input box, stuck UI) behind.
        names = {c.function_name for c in calls}
        if "request_user_input" in names:
            logger.info("request_user_input cancelled by interruption -> closing box")
            for rid in list(pending_inputs):
                fut = pending_inputs.pop(rid, None)
                if fut and not fut.done():
                    fut.cancel()
            await close_input_box()
        if "look" in names:
            logger.info("look cancelled by interruption -> dropping pending frame request")
            for rid in list(pending_frames):
                fut = pending_frames.pop(rid, None)
                if fut and not fut.done():
                    fut.cancel()

    @rtvi.event_handler("on_client_message")
    async def on_client_message(_rtvi, message):
        # The browser posts results back over the data channel: the input box returns
        # {id, value} (value None on cancel); the look tool returns {id, image} (a
        # base64 JPEG, or null if there's no camera / permission was denied). Each
        # resolves its matching parked Future.
        data = message.data or {}
        if message.type == "user_input_response":
            logger.info(f"user_input_response from browser: {data!r}")
            future = pending_inputs.get(data.get("id"))
            if future and not future.done():
                future.set_result(data.get("value"))
        elif message.type == "frame_response":
            img = data.get("image")
            logger.info(
                f"frame_response from browser (id={data.get('id')!r}, "
                f"{'image received' if img else 'no image'})"
            )
            future = pending_frames.get(data.get("id"))
            if future and not future.done():
                future.set_result(img)

    # Optional "agent re-engages when you go quiet" — reinforces story mode's
    # leading feel. Off unless STORY_IDLE_SECS (or the style default) is > 0; each
    # fire is a full-context LLM turn with no user input, so it's the most
    # token-expensive feature here. Lazy import keeps it off the default path.
    idle_secs = float(os.getenv("STORY_IDLE_SECS", str(style_cfg["idle_secs"])))
    user_turn = None
    if idle_secs > 0:
        from pipecat.turns.user_turn_processor import UserTurnProcessor

        user_turn = UserTurnProcessor(user_idle_timeout=idle_secs)

    pipeline_processors = [transport.input(), rtvi, vad, stt]
    if user_turn is not None:
        # After STT / before the user aggregator: it watches turn boundaries and
        # fires on_user_turn_idle when the user goes quiet (suppressed while a
        # function call like request_user_input is still pending).
        pipeline_processors.append(user_turn)
    pipeline_processors += [
        context_aggregator.user(),
        llm,
        tts,
        transport.output(),
        context_aggregator.assistant(),
    ]
    pipeline = Pipeline(pipeline_processors)

    task = PipelineTask(
        pipeline,
        params=PipelineParams(allow_interruptions=True, enable_metrics=True),
        observers=[RTVIObserver(rtvi)],
    )

    @rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        await rtvi.set_bot_ready()
        # Kick off the first turn. Companion kickoff is a simple greeting; story
        # kickoff has Vivid open and take the lead (see STYLES).
        messages.append({"role": "system", "content": style_cfg["kickoff"]})
        await task.queue_frames([LLMRunFrame()])

    if user_turn is not None:

        @user_turn.event_handler("on_user_turn_idle")
        async def on_user_turn_idle(_processor):
            # User went quiet after Vivid finished — nudge the thread forward in one
            # short line instead of restarting (story mode keeps driving). Attached
            # here, not at pipeline build, because it needs task + messages.
            logger.info("User idle -> nudging the conversation forward.")
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "The user has gone quiet. Pick the thread back up in one "
                        "short line — don't restart, just nudge it forward."
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
