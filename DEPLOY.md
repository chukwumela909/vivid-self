# Deploying vivid-self on Coolify

Two services from one repo:

| Service | Stack | Port | Role |
|---|---|---|---|
| `bot/`  | Python 3.12 / FastAPI | 7860 | WebRTC signaling (`/api/offer`) + voice pipeline |
| `web/`  | Next.js (standalone)  | 3000 | Browser client |

You can run either of two media transports, switched by env var:

- **`daily` (recommended for cloud)** — bot and browser join a Daily room;
  Daily provides the media infra (SFU + TURN), so there's **no NAT/ICE/coturn
  setup**. Needs `DAILY_API_KEY`. Free tier ~10,000 participant-min/month.
- **`smallwebrtc`** — peer-to-peer; no third party, but media must traverse NAT
  (see "WebRTC media" below), which means a TURN server in most cloud setups.

Set `TRANSPORT` (bot) and `NEXT_PUBLIC_TRANSPORT` (web) to the **same** value.

---

## 1. Push to GitHub

`.env` is gitignored, so no secrets get committed.

```bash
git init
git add .
git commit -m "Initial commit: vivid-self voice companion"
gh repo create vivid-self --private --source=. --remote=origin --push
```

(Or create the repo on github.com and `git remote add origin … && git push -u origin main`.)

## 2. Connect the repo in Coolify

Coolify → **Sources** → connect your GitHub (GitHub App is easiest), then create
a **Project**.

You can deploy either way:

- **Docker Compose (recommended)** — add a *Docker Compose* resource pointing at
  the repo's `docker-compose.yml`. One resource, both services.
- **Two separate Applications** — add two *Dockerfile* apps, one with Base
  Directory `/bot`, one with `/web`.

## 3. Environment variables (Coolify → each service → Environment)

**bot:**

| Var | Notes |
|---|---|
| `DEEPGRAM_API_KEY` | |
| `GROQ_API_KEY` | |
| `CARTESIA_API_KEY` | |
| `CARTESIA_VOICE_ID` | |
| `GROQ_MODEL` | |
| `DEEPGRAM_MODEL` | defaults to `nova-3` |
| `LLM_PROVIDER` | defaults to `groq` |
| `TRANSPORT` | `daily` or `smallwebrtc` (default) |
| `DAILY_API_KEY` | required when `TRANSPORT=daily` |
| `WEBRTC_ICE_SERVERS` | only for `smallwebrtc`: JSON array of STUN/TURN servers — see below |

**web (all `NEXT_PUBLIC_*` are build-time — set as Coolify *Build Variables*, not runtime env):**

| Var | Notes |
|---|---|
| `NEXT_PUBLIC_BOT_OFFER_URL` | `https://<bot-domain>/api/offer`. For Daily, the bot's `/connect` URL is derived from this same origin. |
| `NEXT_PUBLIC_TRANSPORT` | `daily` or `smallwebrtc` — must match the bot's `TRANSPORT`. |
| `NEXT_PUBLIC_WEBRTC_ICE_SERVERS` | only for `smallwebrtc`: JSON ICE servers, must match the bot's `WEBRTC_ICE_SERVERS`. |

### Using Daily (recommended)

1. Set `TRANSPORT=daily` + `DAILY_API_KEY` on the bot.
2. Set `NEXT_PUBLIC_TRANSPORT=daily` on the web build.
3. Redeploy both. No ICE/TURN config needed — skip the "WebRTC media" section.

Verify: `curl https://<bot-domain>/health` returns `{"status":"ok","transport":"daily"}`.

## 4. Domains & HTTPS

Assign a domain to each service in Coolify; it provisions Let's Encrypt TLS
automatically. **HTTPS is mandatory** — browsers block microphone access and
WebRTC on plain HTTP.

- bot  → `https://bot.yourdomain.com`  (signaling proxied to port 7860)
- web  → `https://app.yourdomain.com`

Set `NEXT_PUBLIC_BOT_OFFER_URL=https://bot.yourdomain.com/api/offer`.

> CORS in `bot/server.py` currently allows `*`. Fine to start; tighten
> `allow_origins` to your web domain when you want.

---

## WebRTC media (the part that needs attention)

Signaling (`/api/offer`) rides HTTPS through Coolify's proxy — that just works.
The **audio** flows separately over UDP, negotiated via ICE, and that does *not*
go through the HTTP proxy. Two facts drive the setup:

1. A **bridged Docker container** only sees its private IP, so it advertises an
   unreachable ICE candidate.
2. aiortc uses **random high UDP ports**, so you can't cleanly "open one port".

Because of this, **STUN alone is unreliable** behind Docker's NAT (it works only
for lenient/full-cone networks). The deterministic fix is a **TURN relay**: both
the browser and the bot send media to TURN's fixed address, sidestepping NAT and
the private-IP problem entirely.

### Quick test (no TURN)

Default `WEBRTC_ICE_SERVERS` is Google STUN:

```json
["stun:stun.l.google.com:19302"]
```

Good enough to confirm the deploy on a permissive network. Expect failures from
clients behind stricter NATs.

### Reliable (TURN via coturn)

1. Uncomment the `coturn` service in `docker-compose.yml` (it uses host
   networking so its ports are directly reachable).
2. Open these on your server firewall: **UDP/TCP 3478** and the relay range
   **UDP 49160–49200**.
3. Set `--external-ip` to your server's public IP and pick `TURN_USER` /
   `TURN_PASSWORD`.
4. Point the bot at it:

   ```json
   [
     "stun:stun.l.google.com:19302",
     {"urls":"turn:turn.yourdomain.com:3478","username":"TURN_USER","credential":"TURN_PASSWORD"}
   ]
   ```

   Set that as `WEBRTC_ICE_SERVERS` on the **bot** service. (The bot returns its
   ICE config to the browser during signaling, so the client uses the same TURN.)

For production, prefer **TURNS over TLS on 443/5349** so relayed media survives
restrictive corporate firewalls.

---

## 5. Verify

1. Open `https://app.yourdomain.com`, click **Start session**, allow the mic.
2. `transport:` should reach `connected`/`ready` and the agent greets you.
3. Hold to talk, speak, release → transcript updates and you hear a reply.
4. If audio never connects but signaling succeeds → it's the media path; add TURN.

## 6. Auto-deploy

Enable Coolify's GitHub webhook so pushes to `main` redeploy automatically.
`NEXT_PUBLIC_BOT_OFFER_URL` only changes the web image, so a rebuild is needed
if you change the bot's domain.
