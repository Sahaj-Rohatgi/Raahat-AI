# Raahat

**Detect early. Support better.**

A demo platform for identifying psychological distress among vulnerable victims and witnesses of crimes — across investigation, trial, rehabilitation and protection — so that support reaches them *before* a crisis, rather than after one.

Built as a working prototype: a FastAPI backend with local ML risk scoring, a deterministic multilingual safety net, and a single-file React frontend with three role-based views.

> ⚠️ **This is a demo system built on synthetic data.** It is not a clinical, legal, or emergency-dispatch tool. Every High/Critical assessment requires human review, and the app never contacts anyone on a user's behalf.

---

## Contents

- [Quickstart](#quickstart)
- [Demo logins](#demo-logins)
- [What it does](#what-it-does)
- [How the risk scoring works](#how-the-risk-scoring-works)
- [Project structure](#project-structure)
- [Phone-in IVR](#phone-in-ivr-no-smartphoneinternet-needed)
- [Configuration](#configuration)
- [Running in production](#running-in-production)
- [API reference](#api-reference)
- [Testing](#testing)
- [Known limitations](#known-limitations)

---

## Quickstart

Requires Python 3.11+ and a running MySQL server 8.0+ (any recent version).

```bash
cd distress-watch-final

# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate                  # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r backend/requirements.txt

# 3. Create the database and point the app at it
mysql -u root -p -e "CREATE DATABASE raahat_dw CHARACTER SET utf8mb4"   # or create it in MySQL Workbench
echo "DATABASE_URL=mysql://<user>:<password>@localhost:3306/raahat_dw" > backend/.env

# 4. Generate synthetic data + train the models (~30s)
python3 -m backend.ml.train

# 5. Run (to preview on a phone, add `--host 0.0.0.0`; see below)
python3 -m uvicorn backend.main:app --reload --port 8001
```

Open **http://127.0.0.1:8001**

### Preview on a phone

For a live preview on a phone connected to the same Wi-Fi as your development computer:

1. Stop the server, then start it listening on your local network:

   ```bash
   python3 -m uvicorn backend.main:app --reload --host 0.0.0.0 --port 8001
   ```

2. Find the computer's local IP address. On macOS, run `ipconfig getifaddr en0` in Terminal. On
   Windows, run `ipconfig` and use the IPv4 address under the Wi-Fi adapter. On Linux, run
   `hostname -I` and use an address on the same Wi-Fi network as the phone.
3. On the phone, open `http://<computer-local-IP>:8001` (for example,
   `http://192.168.1.25:8001`). Keep the server running while previewing.

If it does not load, check that both devices are on the same Wi-Fi and allow the development
server through the computer's firewall. Do not use `127.0.0.1` on the phone: that address refers
to the phone itself. This is for trusted local development only; `0.0.0.0` makes the dev server
reachable by other devices on that network, so stop it when you are done.

Your prompt shows `(.venv)` when the environment is active. Use `deactivate` to exit it, and re-run
`source .venv/bin/activate` in any new terminal — including the one you run the tests from.

The first run creates all tables in `raahat_dw` and seeds one demo case plus the three demo
logins below. Open the database directly in **MySQL Workbench** (or any MySQL client) to inspect every
table — `cases`, `events`, `alerts`, `interventions`, `case_notes`, `users`, `sessions`,
`chat_messages`, `audit_log`, `case_assignments`, `consent` — the same data the `/api/db` endpoint
shows inside the app. `ml.train` writes a ~10 MB model artifact to `backend/ml/artifacts/`. The venv,
`.env`, and artifacts are all gitignored — `.env` holds `DATABASE_URL`, which is per-machine.

> **Passwords are hashed.** `users.password` holds a per-user salted scrypt hash, never a readable
> password, and a database created before this change is re-hashed in place on the next startup.

<details>
<summary>Running without activating (useful for scripts and editors)</summary>

```bash
.venv/bin/python -m uvicorn backend.main:app --reload --port 8001
.venv/bin/python tests/test_api.py
```
</details>

---

## Demo logins

| Role | Email | Password |
|---|---|---|
| Victim / complainant | `victim@demo.in` | `demo123` |
| Counsellor | `counsellor@demo.in` | `demo123` |
| Administrator | `admin@demo.in` | `demo123` |

Once signed in, the floating **Demo view** pill at the top lets you switch roles quickly — it performs a
real re-login as that role's account, not a cosmetic screen swap.

---

## What it does

### Three role-based views

**Victim** — a calm, private space to check in
- Guided 6-step wellbeing check-in (mood, sleep, safety, anxiety, case pressure, support needs)
- Trauma-informed support chatbot
- **One-tap emergency button** that escalates immediately without going through the chatbot
- **Courtroom Rehearsal** — practise giving evidence with live stress biofeedback (see below)
- Personal distress trend, support plan, medication and appointment reminders

**Counsellor** — a caseload workbench
- Priority case list ordered by risk, with live scores
- Per-case detail: distress trend from real event history, risk indicators, interventions, notes, timeline
- **Override the AI assessment** with a logged reason when professional judgement differs
- Export a full case report as a text file
- Cross-case interventions view and all-cases table

**Administrator** — district/state/national oversight
- Risk distribution, open alerts, aggregate metrics
- Drill down national → state → district
- **Alert queue** with resolve / in-progress / assign actions
- Anonymised vulnerable-cases roster (IDs and age bands, not names — by design)

### Courtroom Rehearsal

For many witnesses the most frightening moment of a case is testifying, and the first time they see a
courtroom is the day they have to speak in one. The rehearsal is a practice run built on graded exposure:

1. **Know the room** — an illustrated courtroom; tap the judge, witness box, defence, dock or gallery to
   learn who sits there and what they will do.
2. **Resting pulse** — 20 seconds of calm to set a personal baseline.
3. **Four rounds of questions** (English or Hindi, optionally read aloud): warm-up → your account →
   cross-examination → pressure. Answered out loud (nothing recorded) or typed.
4. **It backs off when stress spikes.** If the pulse stays more than ~15% above baseline for 4 seconds — or
   the person rates a question "Stressed", or taps *I need a pause* — the rehearsal stops, runs a box-breathing
   exercise, measures how long the pulse takes to settle, and returns with an easier question first.
5. **Court readiness score (0–100)**, a pulse-through-the-session chart, and progress across sessions
   ("first session peak 118 bpm → today 92"). Counsellors see the same in the case's *Court readiness* tab,
   including which round the witness still struggles with.

**Pulse from the camera.** A fingertip over the lens (or a face in front of a webcam) changes colour very
slightly with every heartbeat. Each frame is reduced to one number in the browser — no image leaves the
device — and the rate is read from the frequency spectrum of the last 10 seconds, scored on the rate *and*
its harmonic so a fast, stressed pulse is not misread as half its value. Signals with implausibly large
swings (movement, screen flicker) or with power spread across the band (noise) read as "no steady signal"
instead of a confident wrong number. Tested against synthetic pulse waves from 50 to 165 bpm with noise and
frame jitter: worst error 1.3 bpm. Without a camera, the person rates each question instead; a clearly
badged **simulated** sensor exists for presenting on a machine without a usable camera, and is recorded as
such. It is a wellness estimate, never a medical reading.

**Privacy and safety.** Practice answers are never stored and do not move the distress score — only the
session measurements are saved (`rehearsal_sessions`). The one exception mirrors the chat: a typed answer
that the deterministic safety router reads as urgent is a real disclosure, and is escalated exactly like a
chat message. The readiness score is a short explainable rule (`database.rehearsal_readiness`), not a model.

### The safety net

Distress scoring is ML-based, but **urgent safety detection is deterministic and never delegated to a model.**
`services/chatbot.py` runs regex-based routing for six categories — medical emergency, self-harm, violence,
child safeguarding, threats, and abuse — in **English, Hindi (Devanagari), and Hinglish (romanized Hindi)**.

If the safety router fires, the case is forced to Critical regardless of what the ML model thought, an alert
is raised, and the reply comes from a fixed local response — never from an LLM. Negation is handled, so
*"I am not suicidal, just stressed"* does not trigger.

### Escalation notifications

Every High/Critical alert is logged to the server console immediately, pushed live over WebSocket
(`/ws/alerts`) to every connected counsellor/admin dashboard, and, if `ALERT_WEBHOOK_URL` is set, POSTed as
JSON to that endpoint (e.g. a Slack incoming webhook) — so an alert reaches a human instead of waiting for
someone to refresh a dashboard. The frontend opens the socket automatically for staff sessions and shows a
toast the moment a new alert arrives.

### Explainability

Every assessment returned by `ml/predict.py` includes `factors` — the individual signals that drove *this*
score, weighted by the trained model's own `feature_importances_` against the case's actual answers (not a
fixed illustrative list) — plus a `confidence` figure derived from how much the random forest's individual
trees agree on this specific prediction. The counsellor case view renders these as the "Why is this case
{risk band}?" panel, and shows an honest empty state for a case with no assessments yet instead of numbers
that were never computed.

---

## How the risk scoring works

```
      check-in answers ─┐
                        ├──► feature vector ──► RandomForest ──► distress score (0–100)
   missed check-ins ────┤                    ├─► RandomForest ──► escalation probability
                        │                    └─► MultiOutput  ──► recommended interventions
   text ──► TF-IDF ──► LogisticRegression ──► text risk band ──┘
                                    │
                                    └── (independent) ──► deterministic safety router
                                                              │
                                                   urgent? ──► force Critical + alert
```

- **Text model** — TF-IDF (1–2 grams) + logistic regression over synthetic English/Hindi/Hinglish examples,
  producing a risk band used as one feature among several.
- **Score / escalation / interventions** — random forests over the check-in features plus the text risk band.
- **Missed check-ins** — computed from the real gap since a case's last check-in (7-day cadence, capped at 10).
- **Explanations** — every assessment returns real per-factor weights (feature importance × this case's
  actual values) and a confidence score (agreement across the forest's trees), shown in the UI.

> **Tokenizer note:** scikit-learn's default `token_pattern` relies on `\b` word boundaries, which do **not**
> align correctly after Devanagari vowel signs (matras) — it shreds `"सुरक्षित"` into `"रक"`. `ml/train.py`
> therefore uses a custom punctuation-aware pattern that works for all three languages. Without it, Hindi
> support is silently worthless.

---

## Project structure

```
.
├── raahat.html                 # the authored frontend: React + Tailwind, JSX in one file
├── build.mjs                   # transpiles it ahead of time into static/app.<hash>.js
├── static/vendor/              # React, ReactDOM, Tailwind served locally (works offline)
├── Dockerfile                  # multi-stage: frontend build, wheel build, slim runtime
├── docker-compose.yml          # app + MySQL + Redis, production-shaped
├── gunicorn.conf.py            # worker count, timeouts, recycling
├── alembic/                    # schema migrations (non-additive changes live here)
├── .github/workflows/ci.yml    # tests, browser suite, dependency audit, secret scan, image build
├── backend/
│   ├── main.py                 # FastAPI app: routes, middleware, lifecycle
│   ├── config.py               # all configuration, and the production safety checks
│   ├── auth.py                 # sign-in, bearer sessions, role dependencies
│   ├── database.py             # MySQL schema, pool, queries, retention, erasure
│   ├── admin.py                # operational CLI: accounts, sessions, purges, preflight
│   ├── observability.py        # structured logging, request ids, Prometheus metrics
│   ├── ml/
│   │   ├── generate.py         # synthetic data (English + Hindi + Hinglish)
│   │   ├── train.py            # trains and persists all four models
│   │   ├── predict.py          # shared inference + explanations
│   │   ├── forecast.py         # trajectory projection from stored assessments
│   │   ├── data/               # generated CSV (gitignored)
│   │   └── artifacts/          # trained models (gitignored)
│   └── services/
│       ├── chatbot.py          # multilingual safety routing + Gemini replies (circuit-broken)
│       ├── notify.py           # escalation delivery, retries, unacknowledged-alert sweeper
│       ├── realtime.py         # WebSocket fan-out, Redis pub/sub across workers
│       ├── ratelimit.py        # shared sliding-window limits
│       ├── cache.py            # Redis connection, with a documented single-process fallback
│       └── twilio_ivr.py       # TwiML + webhook signature verification (fails closed)
└── tests/
    ├── test_api.py             # 62 backend checks (stdlib only)
    ├── test_production.py      # 37 hardening checks (config, sessions, CSP, erasure)
    ├── test_chatbot.py         # safety-router regressions
    ├── test_voice.py           # voice transcription
    └── e2e.mjs                 # 23 browser checks (Playwright, against the built page)
```

**Database tables:** `cases`, `events`, `alerts`, `interventions`, `case_notes`, `chat_messages`,
`chat_feedback`, `users`, `sessions`, `audit_log`, `consent`, `followups`, `privacy_requests`,
`phone_links`, `case_assignments`, `rehearsal_sessions`.

The baseline schema, additive columns and indexes are created on startup under a MySQL advisory
lock, so concurrent workers in a rolling deploy cannot race each other. Anything non-additive is an
Alembic revision (`alembic upgrade head`), run once before workers start.

---

## Phone-in IVR (no smartphone/internet needed)

For victims/witnesses without a smartphone or reliable data, `backend/ivr/` adds a phone-in keypad
interface on top of the exact same scoring pipeline — no separate business logic, just a different way
in. It runs on **FreeSWITCH**, a free, open-source PBX (no telecom account or phone number needed for the
demo — it's a local softphone-to-softphone call).

### Option A: fully self-hosted, via FreeSWITCH — the default

Every part of this path is open source and self-hosted: FreeSWITCH (MPL 1.1) for the telephony, eSpeak NG
(GPL) for the voice prompts, and the project's own FastAPI backend. There is no account to create, no API
key to obtain, and nothing to pay for.

**One-time setup:**
```bash
brew install freeswitch espeak-ng        # apt install freeswitch espeak-ng on Linux
brew services start freeswitch

echo "IVR_API_KEY=demo-ivr-key" >> backend/.env   # any shared secret; the dialplan reads it from here
bash backend/ivr/prompts/generate.sh              # render the voice prompts (eSpeak NG)
bash backend/ivr/freeswitch/install.sh            # install the dialplan into FreeSWITCH and reload it
```

`install.sh` renders `dialplan_98_raahat_ivr.xml.template` into FreeSWITCH's config directory,
filling in this checkout's absolute path (FreeSWITCH resolves prompt files by absolute path), the
`IVR_API_KEY` from `backend/.env`, and the backend URL. That indirection is why the checked-in dialplan is
a template: a literal dialplan would carry one developer's home directory and the shared secret into
version control. It finds FreeSWITCH's config dir on Apple Silicon brew, Intel brew and Linux; override
with `FS_CONF=/path/to/freeswitch`, and the backend port with `API_BASE=http://127.0.0.1:8080`.

FreeSWITCH's own dialplan/`sip_profiles` ship a few sample entries that need adjusting for a clean local
run (stock `external-ipv6` profile fails to bind on machines without a real routable IPv6 address; the
stock `4000` extension collides with FreeSWITCH's own voicemail demo — which is why this IVR answers on
`7100`; the stock `default_password` triggers a mandatory 10-second per-call nag delay).

**Voice prompts:** `generate.sh` uses **eSpeak NG** by default, so the prompts are reproducible on any OS
rather than only on the machine that first made them. On a Mac, `TTS=say bash backend/ivr/prompts/generate.sh`
swaps in the built-in `say` voice, which sounds noticeably better but is macOS-only. Both write the 8 kHz
mono 16-bit WAV that FreeSWITCH plays without transcoding; the eSpeak path resamples via
`backend/ivr/prompts/resample.py` (standard library only, so no sox/ffmpeg install).

**Testing it:** install a free SIP softphone (e.g. [Linphone](https://www.linphone.org/en/download/)),
register it to server `127.0.0.1`, extension `1001`, password `distresswatch2026` (FreeSWITCH's own default
demo password, changed from `1234` to skip the nag delay above). Dial **7100** — the same IVR menu the
victim app's chat and check-in share:
- Press **1** for a guided wellbeing check-in (mood, sleep, safety, anxiety, case pressure — same fields
  as the web check-in), answered via keypad digits.
- Press **9** for emergency — skips straight to the same Critical escalation as the victim app's one-tap
  emergency button, including the live WebSocket push to staff dashboards.

Every answer is POSTed straight to `/api/ivr/checkin` / `/api/ivr/sos` (see `backend/main.py`), secured by
a shared `IVR_API_KEY` header (a phone call has no web session to check) — from there it's the identical
ML scoring, safety-net, alerting, and audit-log code path as a web check-in or chat message.

### Option B (optional): a real phone number, via Twilio

> **Not open source, and not required.** This path depends on Twilio, a paid third-party service, and is
> the one part of this project that does. Option A above is the supported default and needs none of it —
> skip this section entirely unless you specifically want a number the public telephone network can dial.

The FreeSWITCH path above is fully self-hosted and free, but it can only be reached from a softphone on
the same machine — no real phone can dial it, because a number that the public telephone network can route
has to be rented from a carrier. `/api/twilio/*` is the same IVR behind a **real dialable number**, using
Twilio's free trial credit.

Two practical limits worth knowing before starting: a Twilio **trial expires 30 days after sign-up**, and
Indian phone numbers are not offered on trial accounts (they need a regulatory KYC bundle), so an
India-based demo ends up dialling a US number at international rates.

Twilio inverts the FreeSWITCH arrangement: rather than this server receiving the call, Twilio's servers
POST to *us* at each step and we answer with TwiML describing what to do next. The backend therefore has
to be publicly reachable:

```bash
# 1. Expose the local server (ngrok's free tier is enough)
ngrok http 8001

# 2. Put both credentials from the console.twilio.com dashboard into backend/.env
echo "TWILIO_ACCOUNT_SID=<your account SID>" >> backend/.env
echo "TWILIO_AUTH_TOKEN=<your token>" >> backend/.env

# 3. Point the number's "A call comes in" webhook at the tunnel. This reads the
#    credentials from backend/.env and finds the running ngrok URL by itself:
python3 -m backend.ivr.setup_twilio
```

Step 3 is the one to repeat: ngrok's free tier issues a **new URL every restart**, and the webhook has to
follow it. Run it with no arguments to update every number on the account, or name one
(`python3 -m backend.ivr.setup_twilio +919812345678`); `--url https://example.com` skips ngrok discovery
for a real deployment. Doing it by hand in the console instead works just as well — set "A call comes in"
to `https://<subdomain>.ngrok-free.dev/api/twilio/voice` over **HTTP POST**.

Then dial the number from any phone — same menu, same **1** / **9** options, same backend pipeline as
every other entry point. Twilio's built-in text-to-speech reads the prompts (`Polly.Aditi`, an Indian
English voice, configurable via `TWILIO_VOICE`), so the recorded WAV files are not needed on this path.

**Signature verification:** Twilio signs every webhook, and `/api/twilio/*` rejects anything whose
`X-Twilio-Signature` doesn't verify against `TWILIO_AUTH_TOKEN` — otherwise anyone who found the ngrok URL
could raise fake Critical alerts. With no token configured the check is skipped so the routes can be
exercised with `curl` locally; **set the token before pointing a real number at the server.**

> Trial-account caveats: Twilio plays a short "you have a trial account" recording before connecting, and
> trial credit is finite. Neither matters for a demo, but neither is this free forever — which is exactly
> why the self-hosted FreeSWITCH path above still exists alongside it.

---

## Configuration

Copy `backend/.env.example` to `backend/.env` — it documents every variable inline. Everything is
optional for local development; [Running in production](#running-in-production) lists what becomes
mandatory, and `python3 -m backend.config` checks it for you.

The ones worth knowing about locally:

| Variable | Effect |
|---|---|
| `APP_ENV` | `development` (default) keeps demo seeding, `/docs` and the raw-table inspector on. `production` makes every default strict and refuses to start if anything is unsafe. |
| `GEMINI_API_KEY` | Enables Gemini-generated replies for **non-urgent** chat only. Urgent messages always use local deterministic responses. |
| `GEMINI_MODEL` | Defaults to `gemini-2.5-flash`. |
| `REDIS_URL` | Shares rate-limit counters and WebSocket alert fan-out across workers. Required for more than one worker. |
| `ALERT_WEBHOOK_URL` | High/Critical escalations are POSTed here, off the request path, with retries. |
| `IVR_API_KEY` | Shared secret the FreeSWITCH dialplan sends on `/api/ivr/*`. Unset (or `ENABLE_IVR=0`) and those routes do not exist. |
| `TWILIO_AUTH_TOKEN` | Verifies the signature on `/api/twilio/*` webhooks. **Without it every webhook is rejected** — those endpoints can raise Critical escalations, so they fail closed. |
| `SEED_DEMO_DATA` | Creates the three demo logins below. Refused in production. |

Load them when starting the server:

```bash
python3 -m uvicorn backend.main:app --reload --port 8001 --env-file backend/.env
```

Gemini replies are additionally guarded: the response is rejected if it exceeds a length cap or contains
false claims (e.g. "I contacted someone for you", or promises of confidentiality).

---

---

## Running in production

The application refuses to start with `APP_ENV=production` if any of the following is unsafe
(`backend/config.py`), so a misconfigured deployment fails immediately and loudly rather than
running in a state that looks fine:

| Must be true | Why |
| --- | --- |
| `SEED_DEMO_DATA=0` | The seeded logins have passwords published in this README, and one of them is an administrator. |
| `SESSION_SECRET` ≥ 32 random chars | Pepper for session-token hashing. Rotating it signs everyone out — the correct response to a suspected token leak. |
| `ALLOWED_HOSTS` set | Blocks Host-header poisoning behind a proxy. |
| `ENABLE_DB_INSPECTOR=0` | `/api/db` returns participant chat transcripts. |
| `TWILIO_AUTH_TOKEN` set, if Twilio is on | Webhook signature checking **fails closed**; those endpoints can raise Critical escalations. |
| `IVR_API_KEY` ≥ 32 chars, if the IVR is on | Same reason, for the FreeSWITCH transport. |

Check a deployment before shipping it:

```bash
python3 -m backend.config          # configuration only
python3 -m backend.admin preflight # configuration + database + bundle + webhook warnings
```

### Deploy

```bash
# 1. Build the frontend (no in-browser compiler, strict CSP)
npm ci && npm run build

# 2. Apply schema migrations — once, before any worker starts
alembic upgrade head

# 3. Create the first real administrator (the password is generated, not typed)
python3 -m backend.admin create-user --email ops@example.org --role admin --name "Ops"

# 4. Serve
gunicorn backend.main:app -c gunicorn.conf.py
```

Or the whole stack — app, MySQL, Redis — with `docker compose up --build`. The image pins
`APP_ENV=production`, `SEED_DEMO_DATA=0` and `ENABLE_DB_INSPECTOR=0` so it cannot be started
with demo accounts, and runs as an unprivileged user.

### Required infrastructure

- **TLS terminates upstream.** The app sets HSTS (`ENABLE_HSTS`) but never terminates TLS itself.
  Set `TRUST_PROXY_HEADERS=1` **only** when a proxy you control is in front — otherwise a client can
  forge `X-Forwarded-For` and give itself a fresh rate-limit bucket per request.
- **Redis is required for more than one worker.** Rate-limit counters and the WebSocket alert
  fan-out are shared through it. Without it, the effective rate limit is multiplied by the worker
  count and an escalation only reaches dashboards connected to the worker that raised it.
- **A dedicated MySQL account**, not root, with `SELECT,INSERT,UPDATE,DELETE` and no DDL rights
  once migrations have been applied.
- **`DB_POOL_SIZE` × `WEB_CONCURRENCY` must stay below the server's `max_connections`.**

### Operating it

| Concern | How |
| --- | --- |
| Health | `/healthz` (liveness, no dependencies) and `/readyz` (readiness, checks the database) |
| Metrics | `/metrics`, Prometheus format, restricted to private/loopback addresses in production |
| Logs | One JSON object per line with `LOG_FORMAT=json`, every line carrying the request id also returned as `X-Request-ID`. No message bodies or transcripts are ever logged. |
| Errors | Set `SENTRY_DSN` (with `send_default_pii=False`) |
| Escalation delivery | `ALERT_WEBHOOK_URL` is POSTed off the request path with retries; a permanent failure logs at ERROR and increments `raahat_escalation_delivery_total{outcome="failed"}`. **Alert on that metric** — it means on-call was not paged. |
| Unacknowledged alerts | A High/Critical alert not acknowledged within `ESCALATION_ACK_SECONDS` is automatically re-paged. Staff acknowledge via `POST /api/alerts/{id}/ack`; time-to-acknowledgement is the service level worth tracking. |
| Retention | `DATA_RETENTION_DAYS` plus a scheduled `python3 -m backend.admin purge-data` |
| Session hygiene | `python3 -m backend.admin purge-sessions` on a schedule |
| Leaver process | `python3 -m backend.admin deactivate --email …` revokes every live session immediately |

### Data protection

A participant can export everything held about them (`GET /api/privacy/export`) and request
erasure (`POST /api/privacy/delete-request`). **Erasure is executed, not queued**: participant
content is deleted across every table in one transaction, the login is deactivated, live sessions
are revoked, and the case row is reduced to a tombstone so its id can never be re-used. The
tombstone is deliberate — safeguarding history has to remain attributable, and a recycled case id
would silently inherit someone else's record.

Phone numbers are stored as keyed hashes, never in the clear. Before going live you still need a
DPIA, a stated retention period (`DATA_RETENTION_DAYS`), a documented breach-notification path,
and — under the DPDP Act 2023 — verified consent records and a named grievance officer.

### Before real participants

The engineering is production-shaped; the clinical governance is not something code can supply:

1. A labelled evaluation set with **false-negative rate** as the primary metric — missing someone
   in danger is the failure that matters, not a false alarm.
2. Model calibration, drift monitoring, and a versioned model registry.
3. A staffed on-call rota with an acknowledgement SLA, tested end to end against
   `ALERT_WEBHOOK_URL`.
4. Clinical and legal sign-off on the escalation thresholds and on every piece of user-facing
   safety copy.
5. MFA for staff accounts, and a penetration test.

---

## API reference

All `/api/*` routes except `/api/auth/login` require `Authorization: Bearer <token>`.

| Method | Route | Access | Purpose |
|---|---|---|---|
| `POST` | `/api/auth/login` | public | Exchange email/password for a session token |
| `GET` | `/api/me` | any session | Dashboard payload, **scoped to own case** for a victim |
| `GET` | `/api/dashboard` | staff | Full cross-case dashboard |
| `GET` | `/api/db` | **admin** | Read-only inspector of all database tables (passwords and session tokens are masked) |
| `GET` | `/api/cases` | staff | List all cases |
| `POST` | `/api/cases` | staff | Register a new case |
| `GET` | `/api/cases/{id}` | own case / staff | Single case |
| `POST` | `/api/chat` | any session | Send a chat message, get assessment + reply |
| `POST` | `/api/checkins` | any session | Submit a guided check-in |
| `PATCH` | `/api/alerts/{id}` | staff | Resolve / progress / assign an alert |
| `PATCH` | `/api/cases/{id}/override` | staff | Human override of the AI assessment |
| `POST` | `/api/cases/{id}/notes` | staff | Add a case note |
| `POST` | `/api/cases/{id}/sos` | own case / staff | Emergency escalation (bypasses ML entirely) |
| `GET` | `/api/cases/{id}/report` | staff | Plain-text case report |
| `POST` | `/api/ivr/checkin` | `X-IVR-Key` header | Phone-in check-in (see [Phone-in IVR](#phone-in-ivr-no-smartphoneinternet-needed)) |
| `POST` | `/api/ivr/sos` | `X-IVR-Key` header | Phone-in emergency escalation |
| `POST` | `/api/twilio/voice` | Twilio signature | TwiML entry point for a real phone number |
| `POST` | `/api/twilio/menu` | Twilio signature | Handles the 1 / 9 menu keypress |
| `POST` | `/api/twilio/checkin` | Twilio signature | One check-in question per request |
| `GET` | `/ws/alerts?token=` | staff (query param) | WebSocket: live push of new High/Critical alerts |

**Authorization is enforced server-side, not in the UI.** A victim session that posts another case's
`case_id` is silently pinned back to its own case; reading another case returns `403`.

Interactive API docs: **http://127.0.0.1:8001/docs**

---

## Testing

### Automated

Start the server first, then in a second terminal (activate the venv there too):

```bash
source .venv/bin/activate

# Backend: auth, access control, validation, concurrency, persistence (44 checks, no extra deps)
python3 tests/test_api.py

# Production hardening: config refusal, session-token storage, CSP headers, participant
# scoping, alert acknowledgement, and that an erasure request actually erases (37 checks)
python3 tests/test_production.py

# Safety router and voice transcription (unit tests, no server needed)
python3 tests/test_chatbot.py
python3 tests/test_voice.py

# Frontend: full browser click-through of all three roles (23 checks)
npm install && npx playwright install chromium
node tests/e2e.mjs
```

All of the above run on every push and pull request — see `.github/workflows/ci.yml`, which
also builds the container image, audits dependencies for known vulnerabilities, scans for
committed secrets, and asserts that a production configuration carrying demo accounts is
**refused** rather than started.

Both suites clean up after themselves and leave the demo database as they found it.

The API suite includes a **concurrency test** that fires 20 simultaneous case creations and asserts all
succeed with unique IDs — this previously failed 50% of the time due to an ID-allocation race.

### Manual walkthrough

Open http://127.0.0.1:8001 and follow along. Each step below exercises a specific capability.

**1. Authentication is real**
- Click **Counsellor Login**, change the password to anything wrong, hit **Sign in**
- → *"Invalid email or password"*, and you stay locked out
- Click **Enter Counsellor Demo** to get in properly

**2. Multi-case support**
- In **Priority cases**, click **+ New case**, fill in a name/district/state, **Create case**
- → It appears immediately and opens as a real case
- Click between **Asha K.**, **Rina P.** and your new case
- → Each shows its *own* score, risk badge, trend, and last check-in. The new case correctly shows
  "No check-in yet" with empty interventions/notes

**3. Case notes persist**
- On any case → **notes** tab → type a note → **Add**
- Fully reload the browser (⌘R / F5), sign back in, return to that case's notes
- → The note is still there

**4. Override the AI**
- **overview** tab → **Override AI assessment** (amber button, right column)
- Set a score, pick a band, give a reason → **Save override**
- → Header badge and score update immediately; the override is logged as an auditable event

**5. Export a report**
- **Export report** (top-right of the case panel)
- → Downloads a `.txt` with score, alerts, interventions, notes, event history and the review disclaimer

**6. Counsellor navigation**
- Click **All Cases**, **Interventions**, **Reports** in the left sidebar
- → Each loads real content

**7. Multilingual detection** *(the important one)*
- Switch to **Victim** via the top pill → open the green chat bubble
- Send `मुझे मार रहा है` (or Hinglish `mujhe maar raha hai`)
- → Red emergency card appears; case escalates to Critical
- Now send a **non-urgent** Hindi message: `अदालत की सुनवाई से पहले मुझे चिंता होती है`
- → Normal supportive reply, Moderate-range score — the retrained model grading Hindi properly
- Sanity check: `मैं आज ठीक महसूस कर रही हूं` → stays calm, low score
- Negation check (English): `I am not suicidal, just stressed` → does **not** trigger the emergency card

**7b. Live alert push**
- As Counsellor or Admin, keep the **Alerts** page open in one tab
- From a second tab/session, send an urgent message as Victim (e.g. `mujhe maar raha hai`)
- → The alert appears in the first tab within a second or two, with a toast — no manual refresh, pushed
  over the `/ws/alerts` WebSocket

**8. One-tap emergency**
- Victim home, bottom-right card → **"I need help right now"**
- → Red confirmation banner; case jumps to Critical instantly, no chatbot needed

**9. Alert lifecycle**
- Switch to **Admin** → **Alerts**
- → The Critical alerts from steps 7–8 are waiting
- Click **Mark Resolved** / **In Progress** / **Assign Counsellor** → status persists across reload

**10. Escalation notifications** *(watch your server terminal)*
```
[Raahat] Critical escalation — case 7934 (Asha K.): Safety router detected an urgent message...
```

**11. Access control is server-side**

While signed in as **Victim**, open the browser console:
```js
fetch('/api/dashboard', {headers:{Authorization:'Bearer '+AUTH_TOKEN}}).then(r=>console.log(r.status))
```
→ `403`. A victim account cannot read other people's case data even when bypassing the UI entirely.

---

## Known limitations

Being explicit about what this prototype does **not** do:

- **There is no self-service identity.** Accounts are created by an operator
  (`python3 -m backend.admin create-user`), and there is no sign-up, password reset flow, or MFA.
  Sessions are server-side, token hashes are stored rather than tokens, and an account can be
  signed out everywhere — but MFA for staff accounts is the obvious next requirement.
- **All data is synthetic.** The models are trained on generated examples, not real case data, and their
  accuracy figures mean nothing clinically.
- **District/state aggregates in the admin view are simulated.** They exist to demonstrate the
  national → state → district drill-down, and the dashboard says so on screen. Everything else in the
  app renders only what the database holds: the case lists, the "Key indicators" strip, the trend
  charts and the "Why is this case {band}?" panel are all computed from this case's real events, and a
  case with no events shows an empty state rather than invented numbers.
- **Real-time transport covers alerts only.** New High/Critical alerts push live to staff dashboards over
  `/ws/alerts`; everything else (case lists, notes, interventions) still refreshes on action, not via polling.
- **Hindi/Hinglish coverage is a starting point.** The safety patterns and training examples cover common
  phrasings, not the full range of dialects, code-switching, or regional languages beyond Hindi.
- **The frontend is a single authored file.** `npm run build` transpiles it ahead of time into a
  content-hashed bundle, so production ships no compiler and runs under a strict
  `script-src 'self'` CSP — but it is still one 2,500-line file, and it should be split into
  modules before it grows further.
- **Model quality is the open question.** The classifiers are trained on generated examples. Before
  this is used with real people it needs a labelled evaluation set with **false-negative rate** as
  the primary metric, calibration, drift monitoring, and clinical sign-off — see
  [Running in production](#running-in-production).

### Safety commitments in the current build

- Urgent detection never depends on an LLM or on the ML model
- The system never claims to have contacted anyone on a user's behalf
- The chatbot does not diagnose, prescribe, give definitive legal advice, or promise confidentiality
- High/Critical assessments are explicitly flagged as requiring human review
- Staff can override any AI assessment, and the override is logged
