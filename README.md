# vivid-self — voice companion (v1: voice loop)

Browser voice agent for a tasks/productivity app. v1 proves the **conversational
voice loop**: push-to-talk in the browser → Deepgram STT → Groq LLM → Cartesia TTS,
streamed back over WebRTC, with interruption and a live transcript. App-control tools
and screen-awareness are deferred to later phases (see `PRD.md`).

## Architecture

```
Browser (Next.js, http://localhost:3000)
  Pipecat JS client + SmallWebRTC transport
        │  WebRTC audio + RTVI data channel
        ▼
Python bot (FastAPI, http://localhost:7860)
  SmallWebRTC  →  Deepgram STT (nova-3)  →  Groq LLM  →  Cartesia TTS  →  back
  Silero VAD (turn-taking + interruption), RTVI (transcripts/state to the browser)
```

Provider API keys live **only** in the Python bot process. The browser only does
WebRTC signaling against `/api/offer` — no secrets cross that boundary.

> **Transport note:** v1 uses **SmallWebRTC** because `daily-python` ships no Windows
> wheels. The transport is pluggable; for cloud deploy (Linux), swap in Daily using the
> `DAILY_API_KEY` already in `.env`.

## Prerequisites

- Node.js (tested on v24), Python **3.12** (Pipecat/`daily-python` lack 3.13 wheels)
- Keys in root `.env`: `DEEPGRAM_API_KEY`, `GROQ_API_KEY`, `CARTESIA_API_KEY`,
  `CARTESIA_VOICE_ID` (+ `DAILY_API_KEY` for later cloud use)

## Run (two terminals)

**1. Bot**
```powershell
cd bot
.\.venv\Scripts\python.exe server.py   # serves http://localhost:7860
```
(First-time env setup: `py -3.12 -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt`)

**2. Web**
```powershell
cd web
npm install      # first time
npm run dev      # http://localhost:3000  (uses --webpack; Turbopack lacks native SWC here)
```

## Manual verification (needs a mic + speakers)

1. Open http://localhost:3000, click **Start session**, and **allow microphone** when prompted.
   - `transport:` should move to `connected`/`ready`; the agent greets you (confirms TTS).
2. **Hold "Hold to talk"**, speak a sentence, release.
   - Status cycles idle → listening → thinking → speaking; your words and the reply
     appear in the transcript; you hear the spoken response.
3. While the agent is speaking, hold and speak again → it should stop and listen (**interruption**).
4. In DevTools ▸ Network, confirm **no** Deepgram/Groq/Cartesia keys appear client-side —
   only the `/api/offer` exchange.

## Status / known notes

- **TypeScript:** `npx tsc --noEmit` is clean. Two small workarounds account for
  `@pipecat-ai/client-react` (parcel) bundling its own copy of the client-js types:
  a `useVoiceEvent` wrapper hook and one provider-prop cast in
  `web/src/components/VoiceAgent.tsx`.
- **Dual-model routing** (PRD): architected (single `GROQ_MODEL` now); the LLM layer can
  take a stronger tool-model for command turns in Phase 4 without restructuring.
- The headless preview can't grant mic access, so the full audio loop must be verified
  by a human (steps above).
