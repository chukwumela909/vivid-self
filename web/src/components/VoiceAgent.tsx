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

// @pipecat-ai/client-react bundles its own copy of the RTVIEvent enum (parcel
// inlines it), which is nominally distinct from the one exported by client-js.
// This thin wrapper centralizes the unavoidable cast in one place.
function useVoiceEvent(event: RTVIEvent, handler: (...args: never[]) => void) {
  useRTVIClientEvent(event as never, handler as never);
}

// Visual agent states (PRD §11): idle, listening, thinking, speaking, error.
type AgentStatus = "idle" | "listening" | "thinking" | "speaking" | "error";

type TranscriptLine = { id: number; role: "user" | "bot"; text: string };

const STATUS_META: Record<AgentStatus, { label: string; color: string }> = {
  idle: { label: "Idle", color: "#6b7280" },
  listening: { label: "Listening", color: "#22c55e" },
  thinking: { label: "Thinking", color: "#eab308" },
  speaking: { label: "Speaking", color: "#3b82f6" },
  error: { label: "Error", color: "#ef4444" },
};

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
  const lineId = useRef(0);
  const transcriptEndRef = useRef<HTMLDivElement>(null);

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

  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [lines]);

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
      if (TRANSPORT === "daily") {
        // Ask the bot to create a Daily room + token, then join it.
        const res = await fetch(CONNECT_URL, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
        if (!res.ok) throw new Error(`Bot /connect failed: ${res.status}`);
        await client.connect(await res.json());
      } else {
        await client.connect({ webrtcRequestParams: { endpoint: OFFER_URL } });
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
    await client.disconnect();
    setStatus("idle");
  }, [client]);

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
