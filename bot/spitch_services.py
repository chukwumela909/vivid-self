"""Custom Pipecat STT/TTS services backed by Spitch (https://spitch.app).

Spitch does speech-to-text and text-to-speech for African languages — Yoruba
(yo), Igbo (ig), Hausa (ha), Nigerian-accented English (en), Amharic (am) —
which Deepgram and Cartesia don't cover. These two services let the Yoruba/Igbo
voice modes drop into the existing pipeline right next to the Deepgram/Cartesia
(English/Spanish) path; see bot.py's MODES registry.

Batch, not streaming: the Spitch SDK transcribe/generate calls take/return a
whole audio file, so STT runs once per VAD-delimited utterance (via
SegmentedSTTService, which hands run_stt a complete WAV) and TTS synthesizes the
full reply in one call. That's higher latency than Deepgram/Cartesia's streaming
but is the simplest correct integration. Spitch also offers a streaming API; a
later version can adopt it to cut latency (see the project notes).

The `spitch` package is an OPTIONAL dependency: it's imported lazily so the
English/Spanish modes keep working even when it isn't installed. Constructing
either service without it raises a clear install hint.

Verified against the live API (2026-06-17, spitch SDK 1.50.0):
  * transcribe() returns a `Transcription` model with `.text` (and `.segments`).
  * generate() returns a `BinaryAPIResponse` (use `.read()` for bytes). It
    defaults to MP3, so we pass `format="wav"` to get a 24 kHz RIFF/WAV that the
    pipeline can resample. The response handling stays defensive in case the SDK
    shape changes.
Voices are a fixed set (sade, segun, femi, funmi, amina, …, obinna, ngozi, …);
language is passed separately. See the Spitch dashboard for voice↔language fit.
"""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator

from loguru import logger

from pipecat.frames.frames import ErrorFrame, Frame, TranscriptionFrame
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.services.tts_service import TTSService
from pipecat.utils.time import time_now_iso8601

# Guarded import: keep importing this module cheap and side-effect-free so the
# non-Spitch modes don't need the package installed. The failure (if any) is
# surfaced only when a Spitch service is actually constructed.
try:
    from spitch import Spitch  # type: ignore

    _SPITCH_IMPORT_ERROR: Exception | None = None
except Exception as e:  # ModuleNotFoundError, or a transitive import failure
    Spitch = None  # type: ignore
    _SPITCH_IMPORT_ERROR = e


def _require_spitch() -> None:
    """Raise a clear, actionable error if the spitch package isn't importable."""
    if Spitch is None:
        raise RuntimeError(
            "The 'spitch' package is required for the Yoruba/Igbo voice modes but "
            f"could not be imported ({_SPITCH_IMPORT_ERROR}). Install it with "
            "`pip install spitch` and set SPITCH_API_KEY in .env."
        )


def _extract_audio_bytes(response) -> bytes:
    """Best-effort pull raw audio bytes out of a Spitch generate() response.

    The SDK is generated from the same toolchain as OpenAI's, whose binary
    responses expose `.read()` / `.content`. We try those, then fall back to
    treating the response as bytes directly. Kept permissive on purpose since
    the exact return type is unverified (see module docstring).
    """
    if isinstance(response, (bytes, bytearray)):
        return bytes(response)
    for attr in ("read", "content", "audio", "data"):
        value = getattr(response, attr, None)
        if value is None:
            continue
        if callable(value):
            value = value()
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
    raise TypeError(
        f"Could not extract audio bytes from Spitch response of type {type(response)!r}. "
        "Update _extract_audio_bytes in spitch_services.py to match the SDK."
    )


class SpitchSTTService(SegmentedSTTService):
    """Speech-to-text for African languages via Spitch (batch, per-utterance).

    SegmentedSTTService buffers each VAD-delimited utterance and hands run_stt a
    complete WAV — exactly what Spitch's transcribe() wants. VAD-driven turn
    detection still works the same as with Deepgram; we just lose interim
    (partial) transcripts since this transcribes the whole utterance at once.
    """

    def __init__(self, *, api_key: str, language: str, **kwargs):
        _require_spitch()
        super().__init__(**kwargs)
        self._language = language
        # Sync SDK client; calls are dispatched off the event loop via to_thread.
        self._client = Spitch(api_key=api_key)

    def can_generate_metrics(self) -> bool:
        return True

    def _transcribe_sync(self, audio: bytes) -> str:
        response = self._client.speech.transcribe(language=self._language, content=audio)
        return (getattr(response, "text", "") or "").strip()

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        """Transcribe one WAV utterance with Spitch and yield a TranscriptionFrame."""
        try:
            await self.start_processing_metrics()
            text = await asyncio.to_thread(self._transcribe_sync, audio)
            await self.stop_processing_metrics()

            if text:
                logger.debug(f"Spitch STT [{self._language}]: [{text}]")
                yield TranscriptionFrame(text, self._user_id, time_now_iso8601())
            else:
                logger.warning(f"Spitch STT [{self._language}] returned empty transcript")
        except Exception as e:
            logger.exception("Spitch transcribe failed")
            yield ErrorFrame(error=f"Spitch STT error: {e}")


class SpitchTTSService(TTSService):
    """Text-to-speech for African languages via Spitch (batch synthesis).

    Synthesizes the whole reply in one call, then streams it downstream as audio
    frames. push_start_frame / push_stop_frames let the base class own the
    TTSStarted/TTSStopped bookkeeping (same shape as PiperHttpTTSService).
    """

    def __init__(self, *, api_key: str, language: str, voice: str, **kwargs):
        _require_spitch()
        super().__init__(push_start_frame=True, push_stop_frames=True, **kwargs)
        self._language = language
        self._voice = voice
        self._client = Spitch(api_key=api_key)

    def can_generate_metrics(self) -> bool:
        return True

    def _generate_sync(self, text: str) -> bytes:
        # format="wav" is important: without it Spitch returns MP3 (ID3-tagged),
        # which the WAV-header path downstream would misread as raw PCM (noise).
        # WAV comes back as 24 kHz RIFF; the base helper resamples to the
        # pipeline's output rate. (Verified against the live API, 2026-06-17.)
        response = self._client.speech.generate(
            text=text, language=self._language, voice=self._voice, format="wav"
        )
        return _extract_audio_bytes(response)

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        """Synthesize `text` with Spitch and stream it as resampled audio frames."""
        logger.debug(f"{self}: Spitch TTS [{self._language}/{self._voice}] [{text}]")
        try:
            await self.start_tts_usage_metrics(text)
            audio = await asyncio.to_thread(self._generate_sync, text)

            async def _single_chunk() -> AsyncIterator[bytes]:
                # Spitch returns the whole file at once; one chunk is enough —
                # the base helper handles WAV-header stripping and resampling to
                # the pipeline's output rate.
                yield audio

            async for frame in self._stream_audio_frames_from_iterator(
                _single_chunk(), strip_wav_header=True, context_id=context_id
            ):
                await self.stop_ttfb_metrics()
                yield frame
        except Exception as e:
            logger.exception("Spitch generate failed")
            yield ErrorFrame(error=f"Spitch TTS error: {e}")
        finally:
            await self.stop_ttfb_metrics()
