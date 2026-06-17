"use client";

import {
  type ComponentProps,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { PipecatClient, RTVIEvent } from "@pipecat-ai/client-js";
import {
  SmallWebRTCTransport,
  WavMediaManager,
} from "@pipecat-ai/small-webrtc-transport";
import { DailyTransport } from "@pipecat-ai/daily-transport";
import {
  PipecatClientProvider,
  PipecatClientAudio,
  usePipecatClient,
  usePipecatClientTransportState,
  useRTVIClientEvent,
} from "@pipecat-ai/client-react";

const OFFER_URL =
  process.env.NEXT_PUBLIC_BOT_OFFER_URL ?? "http://localhost:7860/api/offer";

// ICE servers for the browser peer connection. Without these the browser only
// gathers host/LAN candidates and never connects to a bot behind NAT/Docker.
// Set NEXT_PUBLIC_WEBRTC_ICE_SERVERS (build-time JSON) to add TURN in prod —
// must match the bot's WEBRTC_ICE_SERVERS. Defaults to public STUN.
function parseIceServers(): RTCIceServer[] {
  const raw = process.env.NEXT_PUBLIC_WEBRTC_ICE_SERVERS;
  const fallback: RTCIceServer[] = [{ urls: "stun:stun.l.google.com:19302" }];
  if (!raw) return fallback;
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) && parsed.length ? parsed : fallback;
  } catch {
    console.warn("[voice] NEXT_PUBLIC_WEBRTC_ICE_SERVERS is not valid JSON; using STUN.");
    return fallback;
  }
}

const ICE_SERVERS = parseIceServers();

// Which transport the browser uses. Must match the bot's TRANSPORT env.
// "daily" joins a Daily room (managed infra, no NAT issues); "smallwebrtc" is
// peer-to-peer against the bot's /api/offer endpoint.
const TRANSPORT = (process.env.NEXT_PUBLIC_TRANSPORT ?? "smallwebrtc").toLowerCase();

// For Daily, the bot exposes /connect (returns room URL + token) instead of
// /api/offer. Derive it from the offer URL's origin so only one URL is configured.
const CONNECT_URL = (() => {
  try {
    return new URL("/connect", OFFER_URL).toString();
  } catch {
    return "http://localhost:7860/connect";
  }
})();

// Voice languages the user can switch between. `value` is the bot's mode (the
// ?lang= query param read by server.py -> run_bot). english/spanish run on the
// existing Deepgram+Cartesia stack; yoruba/igbo run on Spitch (needs
// SPITCH_API_KEY on the bot). Switching reconnects with the new stack.
const LANGUAGES = [
  { value: "english", label: "English" },
  { value: "nigerian_english", label: "Nigerian English" },
  { value: "pidgin", label: "Pidgin" },
  { value: "english_eleven", label: "English (ElevenLabs)" },
  { value: "spanish", label: "Español" },
  { value: "yoruba", label: "Yorùbá" },
  { value: "igbo", label: "Igbo" },
] as const;
const DEFAULT_LANG = "english";
const LANG_STORAGE_KEY = "vivid:lang";

// Conversation styles (the bot's ?style= param -> run_bot). "companion" is the
// default everyday persona; "story" has Vivid lead the conversation while you
// mostly answer and flow. Switching reconnects with a fresh bot-side context,
// exactly like switching language.
const STYLES = [
  { value: "companion", label: "Companion" },
  { value: "story", label: "Story" },
] as const;
const DEFAULT_STYLE = "companion";
const STYLE_STORAGE_KEY = "vivid:style";

// Modes whose TTS voice is chosen in the UI (ElevenLabs). Must match the
// voice_configurable modes in bot.py.
const VOICE_MODES = new Set<string>(["english_eleven"]);
const VOICE_STORAGE_KEY = "vivid:voice";

// Longest-side pixel cap for a captured camera frame before it's sent to the bot's
// vision model. Smaller = fewer vision tokens and a faster upload; 768 is plenty
// for "what is this" recognition and reading short text.
const MAX_FRAME_PX = 768;

// Endpoint that lists the bot's ElevenLabs voices for the picker.
const VOICES_URL = (() => {
  try {
    return new URL("/elevenlabs/voices", OFFER_URL).toString();
  } catch {
    return "http://localhost:7860/elevenlabs/voices";
  }
})();

// Build the bot endpoint with the chosen language (and optional TTS voice). The
// bot builds a fresh pipeline per connection, so these are read at connect time.
function buildEndpoint(
  url: string,
  lang: string,
  voice?: string,
  style?: string,
): string {
  const u = new URL(url);
  u.searchParams.set("lang", lang);
  if (voice) u.searchParams.set("voice", voice);
  if (style) u.searchParams.set("style", style);
  return u.toString();
}

// @pipecat-ai/client-react bundles its own copy of the RTVIEvent enum (parcel
// inlines it), which is nominally distinct from the one exported by client-js.
// This thin wrapper centralizes the unavoidable cast in one place.
function useVoiceEvent(event: RTVIEvent, handler: (...args: never[]) => void) {
  useRTVIClientEvent(event as never, handler as never);
}

// Visual agent states (PRD §11): idle, listening, thinking, speaking, error.
type AgentStatus = "idle" | "listening" | "thinking" | "speaking" | "error";

type TranscriptLine = { id: number; role: "user" | "bot"; text: string };

// A pending request from the bot to show a text-input box (request_user_input).
type InputRequest = { id: string; prompt: string; label?: string };

const STATUS_META: Record<AgentStatus, { label: string; color: string }> = {
  idle: { label: "Idle", color: "#6b7280" },
  listening: { label: "Listening", color: "#22c55e" },
  thinking: { label: "Thinking", color: "#eab308" },
  speaking: { label: "Speaking", color: "#3b82f6" },
  error: { label: "Error", color: "#ef4444" },
};

/**
 * Modal text box the bot pops up via the request_user_input tool. Owns its own
 * draft text so keystrokes don't re-render the whole voice UI. onSubmit receives
 * the typed value; onCancel passes null back to the bot.
 */
function InputModal({
  request,
  onSubmit,
  onCancel,
}: {
  request: InputRequest;
  onSubmit: (value: string) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      onKeyDown={(e) => {
        if (e.key === "Escape") onCancel();
      }}
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (value.trim()) onSubmit(value.trim());
        }}
        className="w-full max-w-sm rounded-xl bg-white p-5 shadow-xl dark:bg-gray-900"
      >
        <p className="mb-3 text-sm font-medium text-gray-900 dark:text-gray-100">
          {request.prompt || "Type a value"}
        </p>
        <input
          ref={inputRef}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder={request.label ?? ""}
          className="mb-4 w-full rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 outline-none focus:border-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100"
        />
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            className="rounded-md border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-600"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={!value.trim()}
            className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
          >
            Done
          </button>
        </div>
      </form>
    </div>
  );
}

/**
 * Inner UI. Assumes it is rendered inside <PipecatClientProvider>.
 * Hands-free conversation: status + transcript via Pipecat RTVI events, with the
 * bot's Silero VAD driving turn-taking once the mic is open.
 */
function VoiceAgentInner() {
  const client = usePipecatClient();
  const transportState = usePipecatClientTransportState();
  const [status, setStatus] = useState<AgentStatus>("idle");
  const [conversing, setConversing] = useState(false);
  const [lines, setLines] = useState<TranscriptLine[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [inputRequest, setInputRequest] = useState<InputRequest | null>(null);
  const [lang, setLang] = useState<string>(DEFAULT_LANG);
  const [switching, setSwitching] = useState(false);
  // Always holds the latest language so connect() (a stable callback) reads the
  // current choice without being re-created on every change.
  const langRef = useRef(lang);
  const [style, setStyle] = useState<string>(DEFAULT_STYLE);
  // Same latest-value ref trick as langRef so connect() reads the current style.
  const styleRef = useRef(style);
  const [voice, setVoice] = useState<string>("");
  const [voices, setVoices] = useState<
    { id: string; name: string; accent: string; category?: string }[]
  >([]);
  const voiceRef = useRef("");
  const lineId = useRef(0);
  const transcriptEndRef = useRef<HTMLDivElement>(null);
  // Camera for the bot's `look` tool. We never add a video track to the WebRTC peer
  // (audio-only via WavMediaManager); instead we grab a single frame on demand,
  // downscale it, and send it over the data channel. The stream is opened lazily the
  // first time the bot asks to look and stopped on disconnect/unmount.
  const videoRef = useRef<HTMLVideoElement>(null);
  const camStreamRef = useRef<MediaStream | null>(null);
  const [camActive, setCamActive] = useState(false);

  const showVoicePicker = VOICE_MODES.has(lang);

  // Restore the user's last language + voice on mount (persists across reloads).
  useEffect(() => {
    try {
      const saved = localStorage.getItem(LANG_STORAGE_KEY);
      if (saved) {
        setLang(saved);
        langRef.current = saved;
      }
      const savedStyle = localStorage.getItem(STYLE_STORAGE_KEY);
      if (savedStyle) {
        setStyle(savedStyle);
        styleRef.current = savedStyle;
      }
      const savedVoice = localStorage.getItem(VOICE_STORAGE_KEY);
      if (savedVoice) {
        setVoice(savedVoice);
        voiceRef.current = savedVoice;
      }
    } catch {
      // localStorage unavailable (private mode / SSR) — fall back to defaults.
    }
  }, []);

  // When a voice-configurable mode is active, load the bot's ElevenLabs voices
  // and default to the first (the bot sorts Nigerian voices to the top).
  useEffect(() => {
    if (!showVoicePicker || voices.length) return;
    let cancelled = false;
    fetch(VOICES_URL)
      .then((r) => r.json())
      .then((d: { voices?: { id: string; name: string; accent: string; category?: string }[] }) => {
        if (cancelled) return;
        const list = d.voices ?? [];
        setVoices(list);
        if (!voiceRef.current && list.length) {
          setVoice(list[0].id);
          voiceRef.current = list[0].id;
        }
      })
      .catch(() => {
        /* bot offline or no key — picker just stays empty */
      });
    return () => {
      cancelled = true;
    };
  }, [showVoicePicker, voices.length]);

  const connected = transportState === "connected" || transportState === "ready";
  const connecting =
    transportState === "connecting" ||
    transportState === "authenticating" ||
    transportState === "initializing";

  const addLine = useCallback((role: "user" | "bot", text: string) => {
    setLines((prev) => {
      // Each transcript event is a complete utterance. Drop exact consecutive
      // duplicates (e.g. React StrictMode double-firing the handler in dev).
      const last = prev[prev.length - 1];
      if (last && last.role === role && last.text === text) return prev;
      return [...prev, { id: lineId.current++, role, text }];
    });
  }, []);

  // Open the camera (once) and return a <video> with frames flowing. Prefers the
  // rear camera on mobile; desktop just has the one. Rejects if the user denies
  // permission or has no camera — captureFrame turns that into a "no image".
  const ensureCamera = useCallback(async (): Promise<HTMLVideoElement> => {
    const v = videoRef.current;
    if (!v) throw new Error("camera video element not mounted");
    if (!camStreamRef.current) {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: "environment" } },
      });
      camStreamRef.current = stream;
      v.srcObject = stream;
      setCamActive(true);
    }
    // Wait until a frame is actually decodable so drawImage has real pixels.
    if (v.readyState < 2) {
      await v.play().catch(() => {});
      await new Promise<void>((resolve) => {
        if (v.readyState >= 2) return resolve();
        const onReady = () => {
          v.removeEventListener("loadeddata", onReady);
          resolve();
        };
        v.addEventListener("loadeddata", onReady);
      });
    }
    return v;
  }, []);

  // Stop the camera and clear the "on" indicator.
  const stopCamera = useCallback(() => {
    camStreamRef.current?.getTracks().forEach((t) => t.stop());
    camStreamRef.current = null;
    if (videoRef.current) videoRef.current.srcObject = null;
    setCamActive(false);
  }, []);

  // Grab one frame for the bot's `look` tool: draw the current video frame to a
  // canvas, downscale (longest side -> MAX_FRAME_PX), JPEG-encode, and send the
  // base64 back over the data channel. On any failure send image:null so the bot's
  // parked tool call resolves as "no image" rather than waiting out its timeout.
  const captureFrame = useCallback(
    async (id: string) => {
      try {
        const video = await ensureCamera();
        const vw = video.videoWidth;
        const vh = video.videoHeight;
        if (!vw || !vh) throw new Error("no video frame yet");
        const scale = Math.min(1, MAX_FRAME_PX / Math.max(vw, vh));
        const w = Math.round(vw * scale);
        const h = Math.round(vh * scale);
        const canvas = document.createElement("canvas");
        canvas.width = w;
        canvas.height = h;
        const ctx = canvas.getContext("2d");
        if (!ctx) throw new Error("no 2d canvas context");
        ctx.drawImage(video, 0, 0, w, h);
        const b64 = canvas.toDataURL("image/jpeg", 0.6).split(",")[1] || null;
        client?.sendClientMessage("frame_response", { id, image: b64 });
      } catch (e) {
        console.warn("[voice] camera frame capture failed:", e);
        client?.sendClientMessage("frame_response", { id, image: null });
      }
    },
    [client, ensureCamera],
  );

  // --- RTVI events -> status + transcript ----------------------------------
  useVoiceEvent(
    RTVIEvent.UserStartedSpeaking,
    useCallback(() => setStatus("listening"), []),
  );
  useVoiceEvent(
    RTVIEvent.UserStoppedSpeaking,
    useCallback(() => setStatus("thinking"), []),
  );
  useVoiceEvent(
    RTVIEvent.BotStartedSpeaking,
    useCallback(() => setStatus("speaking"), []),
  );
  useVoiceEvent(
    RTVIEvent.BotStoppedSpeaking,
    useCallback(() => setStatus("idle"), []),
  );
  useVoiceEvent(
    RTVIEvent.UserTranscript,
    useCallback(
      (data: { text: string; final: boolean }) => {
        if (data.final && data.text.trim()) addLine("user", data.text);
      },
      [addLine],
    ),
  );
  useVoiceEvent(
    RTVIEvent.BotTranscript,
    useCallback(
      (data: { text: string }) => {
        if (data.text) addLine("bot", data.text);
      },
      [addLine],
    ),
  );
  useVoiceEvent(
    RTVIEvent.Error,
    useCallback((e: unknown) => {
      setStatus("error");
      setError(typeof e === "string" ? e : "Voice pipeline error");
    }, []),
  );
  // The bot drives the input box (request_user_input tool) via server messages:
  // one to open it, one to close it (on timeout, or when the user speaks over it
  // / asks to remove it). close_user_input with no id closes whatever's open.
  useVoiceEvent(
    RTVIEvent.ServerMessage,
    useCallback((data: { type?: string; id?: string; prompt?: string; label?: string }) => {
      if (data?.type === "request_user_input" && data.id) {
        setInputRequest({ id: data.id, prompt: data.prompt ?? "", label: data.label });
      } else if (data?.type === "close_user_input") {
        setInputRequest((cur) => (!data.id || cur?.id === data.id ? null : cur));
      } else if (data?.type === "capture_frame" && data.id) {
        // The bot's `look` tool is asking for a camera frame — grab and return one.
        void captureFrame(data.id);
      }
    }, [captureFrame]),
  );

  // Reply to a request_user_input box: send the typed value back (null = cancel)
  // and dismiss the modal. The bot's parked tool call resolves with this value.
  const respondToInput = useCallback(
    (id: string, value: string | null) => {
      client?.sendClientMessage("user_input_response", { id, value });
      setInputRequest(null);
    },
    [client],
  );

  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [lines]);

  // Release the camera if we unmount mid-session.
  useEffect(() => () => stopCamera(), [stopCamera]);

  // --- session control -----------------------------------------------------
  const connect = useCallback(async () => {
    if (!client) return;
    setError(null);

    // Run client.connect() directly in the click gesture (no awaits before it) so
    // the browser keeps user activation — the media layer creates an AudioContext
    // that must be allowed to resume. The mic permission prompt appears here.
    const watchdog = setTimeout(() => {
      setError(
        "Still connecting to the voice bot… make sure you allowed the microphone, and that the bot is running on http://localhost:7860.",
      );
    }, 12000);
    try {
      // Only send a voice for voice-configurable modes (ElevenLabs); others ignore it.
      const v = VOICE_MODES.has(langRef.current) ? voiceRef.current : undefined;
      if (TRANSPORT === "daily") {
        // Ask the bot to create a Daily room + token, then join it.
        const res = await fetch(
          buildEndpoint(CONNECT_URL, langRef.current, v, styleRef.current),
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: "{}",
          },
        );
        if (!res.ok) throw new Error(`Bot /connect failed: ${res.status}`);
        await client.connect(await res.json());
      } else {
        await client.connect({
          webrtcRequestParams: {
            endpoint: buildEndpoint(OFFER_URL, langRef.current, v, styleRef.current),
          },
        });
      }
      // Listen continuously so the bot's wake filter can hear "Hey Vivid".
      client.enableMic(true);
      setConversing(true);
      setStatus("idle");
      setError(null);
    } catch (e) {
      console.error("[voice] connect failed:", e);
      setStatus("error");
      const name = e instanceof Error ? e.name : "";
      setError(
        name === "NotAllowedError"
          ? "Microphone blocked. Allow it via the address-bar icon, reload, and try again."
          : name === "NotFoundError"
            ? "No microphone found. Connect one and try again."
            : e instanceof Error
              ? e.message
              : "Failed to connect to the bot",
      );
    } finally {
      clearTimeout(watchdog);
    }
  }, [client]);

  const disconnect = useCallback(async () => {
    if (!client) return;
    setConversing(false);
    stopCamera();
    await client.disconnect();
    setStatus("idle");
  }, [client, stopCamera]);

  // Change the voice language. Persists the choice immediately so it applies to
  // the next connect. If a session is live, reconnect so the bot rebuilds its
  // pipeline with the new STT/TTS stack (Pipecat can't hot-swap a running
  // graph). Note: a switch starts a fresh bot-side conversation context.
  const switchLanguage = useCallback(
    async (next: string) => {
      if (next === langRef.current) return;
      setLang(next);
      langRef.current = next;
      try {
        localStorage.setItem(LANG_STORAGE_KEY, next);
      } catch {
        // Non-fatal: choice just won't persist across reloads.
      }
      if (!client || !connected) return; // not live yet — applies on next connect
      setSwitching(true);
      try {
        await client.disconnect();
        await connect();
      } finally {
        setSwitching(false);
      }
    },
    [client, connected, connect],
  );

  // Change the ElevenLabs voice (same reconnect dance as switching language).
  const switchVoice = useCallback(
    async (next: string) => {
      if (next === voiceRef.current) return;
      setVoice(next);
      voiceRef.current = next;
      try {
        localStorage.setItem(VOICE_STORAGE_KEY, next);
      } catch {
        // Non-fatal: choice just won't persist across reloads.
      }
      if (!client || !connected) return;
      setSwitching(true);
      try {
        await client.disconnect();
        await connect();
      } finally {
        setSwitching(false);
      }
    },
    [client, connected, connect],
  );

  // Change the conversation style (companion/story). Same reconnect dance as
  // language — the bot rebuilds with the new persona and a fresh context.
  const switchStyle = useCallback(
    async (next: string) => {
      if (next === styleRef.current) return;
      setStyle(next);
      styleRef.current = next;
      try {
        localStorage.setItem(STYLE_STORAGE_KEY, next);
      } catch {
        // Non-fatal: choice just won't persist across reloads.
      }
      if (!client || !connected) return;
      setSwitching(true);
      try {
        await client.disconnect();
        await connect();
      } finally {
        setSwitching(false);
      }
    },
    [client, connected, connect],
  );

  // Hands-free conversation: open the mic and let the bot's VAD drive turns.
  // The wake word will later call startConversation() instead of a button.
  const startConversation = useCallback(() => {
    if (!client || !connected) return;
    client.enableMic(true);
    setConversing(true);
  }, [client, connected]);

  const stopConversation = useCallback(() => {
    if (!client) return;
    client.enableMic(false);
    setConversing(false);
    setStatus("idle");
  }, [client]);

  const meta = STATUS_META[status];

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-6 p-6">
      <header className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">vivid-self · voice</h1>
        <div className="flex items-center gap-2 text-sm">
          <span
            className="inline-block h-3 w-3 rounded-full"
            style={{ backgroundColor: meta.color }}
            aria-hidden
          />
          <span>{meta.label}</span>
        </div>
      </header>

      {/* Connection controls */}
      <div className="flex items-center gap-3">
        {!connected ? (
          <button
            onClick={connect}
            disabled={connecting}
            className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
          >
            {connecting ? "Connecting…" : "Start session"}
          </button>
        ) : (
          <button
            onClick={disconnect}
            className="rounded-md border border-gray-400 px-4 py-2 text-sm font-medium"
          >
            End session
          </button>
        )}

        {/* Language switcher. Changing it reconnects the session with the new
            STT/TTS stack; works before a session too (applies on connect). */}
        <label className="sr-only" htmlFor="voice-lang">
          Voice language
        </label>
        <select
          id="voice-lang"
          value={lang}
          onChange={(e) => switchLanguage(e.target.value)}
          disabled={connecting || switching}
          className="rounded-md border border-gray-300 px-2 py-2 text-sm disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800"
        >
          {LANGUAGES.map((l) => (
            <option key={l.value} value={l.value}>
              {l.label}
            </option>
          ))}
        </select>

        {/* ElevenLabs voice picker — only for voice-configurable modes. */}
        {showVoicePicker && (
          <select
            aria-label="Voice"
            value={voice}
            onChange={(e) => switchVoice(e.target.value)}
            disabled={connecting || switching || voices.length === 0}
            className="rounded-md border border-gray-300 px-2 py-2 text-sm disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800"
          >
            {voices.length === 0 ? (
              <option>Loading voices…</option>
            ) : (
              voices.map((v) => (
                <option key={v.id} value={v.id}>
                  {v.name}
                  {v.accent ? ` · ${v.accent}` : ""}
                  {v.category && v.category !== "premade" ? " — paid plan" : ""}
                </option>
              ))
            )}
          </select>
        )}

        {/* Conversation style — Companion (default) or Story (Vivid leads).
            Switching reconnects, which restarts the conversation. */}
        <label className="sr-only" htmlFor="voice-style">
          Conversation style
        </label>
        <select
          id="voice-style"
          value={style}
          onChange={(e) => switchStyle(e.target.value)}
          disabled={connecting || switching}
          title="Switching restarts the conversation"
          className="rounded-md border border-gray-300 px-2 py-2 text-sm disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800"
        >
          {STYLES.map((s) => (
            <option key={s.value} value={s.value}>
              {s.label}
            </option>
          ))}
        </select>

        {switching && (
          <span className="text-xs text-gray-500">Switching…</span>
        )}

        {camActive && (
          <span
            className="text-xs text-gray-500"
            title="Camera is on for the look tool"
          >
            📷 camera on
          </span>
        )}

        <span className="text-xs text-gray-500">transport: {transportState}</span>
      </div>

      {/* Mic mute toggle. The bot's wake filter listens for "Hey Vivid". */}
      <button
        disabled={!connected}
        onClick={conversing ? stopConversation : startConversation}
        className={`select-none rounded-xl px-6 py-10 text-lg font-semibold transition-colors disabled:opacity-40 ${
          conversing
            ? "bg-green-600 text-white"
            : "bg-gray-200 text-gray-800 dark:bg-gray-800 dark:text-gray-100"
        }`}
      >
        {conversing
          ? "Listening — say “Hey Vivid” · tap to mute"
          : "Muted — tap to listen"}
      </button>

      {error && (
        <p className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>
      )}

      {/* Transcript / event log */}
      <div className="flex h-80 flex-col gap-2 overflow-y-auto rounded-lg border border-gray-200 p-4 dark:border-gray-700">
        {lines.length === 0 ? (
          <p className="text-sm text-gray-400">
            Transcript will appear here. Start a session, then say “Hey Vivid” —
            the agent wakes up and you can just speak.
          </p>
        ) : (
          lines.map((l) => (
            <div key={l.id} className="text-sm">
              <span
                className={
                  l.role === "user"
                    ? "font-medium text-gray-900 dark:text-gray-100"
                    : "font-medium text-blue-600"
                }
              >
                {l.role === "user" ? "You" : "Agent"}:
              </span>{" "}
              <span className="text-gray-700 dark:text-gray-300">{l.text}</span>
            </div>
          ))
        )}
        <div ref={transcriptEndRef} />
      </div>

      {inputRequest && (
        <InputModal
          request={inputRequest}
          onSubmit={(value) => respondToInput(inputRequest.id, value)}
          onCancel={() => respondToInput(inputRequest.id, null)}
        />
      )}

      {/* Hidden camera sink for the `look` tool. Frames are grabbed to a canvas on
          demand; this stream is never added to the WebRTC peer connection. */}
      <video ref={videoRef} className="hidden" muted playsInline />
    </div>
  );
}

/**
 * Top-level component: owns the PipecatClient instance and provides it.
 * Rendered client-only (see page.tsx dynamic import) so the browser-only
 * transport is never constructed during SSR.
 */
export default function VoiceAgent() {
  // Create the client only in the browser (never during SSR), since the
  // SmallWebRTC transport constructs browser-only WebRTC objects.
  const [client, setClient] = useState<PipecatClient | null>(null);

  useEffect(() => {
    const c = new PipecatClient({
      // SmallWebRTCTransport defaults to DailyMediaManager, which spins up a
      // Daily call object (loads Daily's call-machine bundle) and hangs on a
      // pure SmallWebRTC setup. WavMediaManager uses plain browser media.
      transport:
        TRANSPORT === "daily"
          ? new DailyTransport()
          : new SmallWebRTCTransport({
              mediaManager: new WavMediaManager(),
              iceServers: ICE_SERVERS,
            }),
      enableMic: false, // connect receive-only; PTT adds the mic track live
      enableCam: false,
    });
    setClient(c);
    return () => {
      c.disconnect().catch(() => {});
    };
  }, []);

  if (!client) {
    return <div className="p-6 text-sm text-gray-400">Loading voice…</div>;
  }

  return (
    // client-react bundles its own PipecatClient type (parcel-inlined), nominally
    // distinct from client-js's. Cast to exactly the prop type it expects.
    <PipecatClientProvider
      client={
        client as unknown as ComponentProps<
          typeof PipecatClientProvider
        >["client"]
      }
    >
      <VoiceAgentInner />
      {/* Plays the bot's TTS audio track. */}
      <PipecatClientAudio />
    </PipecatClientProvider>
  );
}
