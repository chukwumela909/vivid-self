# Deploying vivid-self on Coolify

Two services from one repo:

| Service | Stack | Port | Role |
|---|---|---|---|
| `bot/`  | Python 3.12 / FastAPI | 7860 | WebRTC signaling (`/api/offer`) + voice pipeline |
| `web/`  | Next.js (standalone)  | 3000 | Browser client |

The transport stays **SmallWebRTC** — no switch to Daily required. The only
production concern is the WebRTC **media** path (see "WebRTC media" below).

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
| `WEBRTC_ICE_SERVERS` | JSON array of STUN/TURN servers — see below |

**web:**

| Var | Notes |
|---|---|
| `NEXT_PUBLIC_BOT_OFFER_URL` | **Build-time.** Set as a *Build Variable* in Coolify, value `https://<bot-domain>/api/offer`. Next.js inlines `NEXT_PUBLIC_*` at build, so a runtime-only var won't work. |

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
