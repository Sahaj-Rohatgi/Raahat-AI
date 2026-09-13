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

# 5. Run
python3 -m uvicorn backend.main:app --reload --port 8001
```

Open **http://127.0.0.1:8001**

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
├── raahat.html          # entire frontend: React + Tailwind, JSX compiled in-browser
├── static/vendor/              # React, ReactDOM, Babel, Tailwind served locally (works offline)
├── README.md
├── backend/
│   ├── main.py                 # FastAPI app: routes, request validation, authorization
│   ├── auth.py                 # demo login, bearer sessions, role dependencies
│   ├── database.py             # MySQL schema, migrations, queries
│   ├── requirements.txt
│   ├── .env.example
│   ├── ml/
│   │   ├── generate.py         # synthetic data (English + Hindi + Hinglish)
│   │   ├── train.py            # trains and persists all four models
│   │   ├── predict.py          # shared inference + explanations
│   │   ├── data/               # generated CSV (gitignored)
│   │   └── artifacts/          # trained models (gitignored)
│   └── services/
│       ├── chatbot.py          # multilingual safety routing + optional Gemini replies
│       └── notify.py           # escalation notifications
└── tests/
    ├── test_api.py             # 43 backend checks (stdlib only)
    └── e2e.mjs                 # 23 browser checks (Playwright)
```

**Database tables:** `cases`, `events`, `alerts`, `interventions`, `case_notes`.
Schema migrations run automatically on startup — existing databases are upgraded in place, not dropped.

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

Copy `backend/.env.example` to `backend/.env`. All values are optional.

| Variable | Effect |
|---|---|
| `GEMINI_API_KEY` | Enables Gemini-generated replies for **non-urgent** chat only. Urgent messages always use local deterministic responses. |
| `GEMINI_MODEL` | Defaults to `gemini-2.5-flash`. |
| `ALERT_WEBHOOK_URL` | If set, High/Critical escalations are POSTed here as JSON. |
| `IVR_API_KEY` | Shared secret the local FreeSWITCH dialplan sends on `/api/ivr/*` calls. **No default** — if it is unset, every `/api/ivr/*` call is rejected with 403. Only needed for the phone-in IVR (see above). |
| `TWILIO_AUTH_TOKEN` | Verifies the signature on `/api/twilio/*` webhooks. Unset = signature check skipped (local testing only). |
| `TWILIO_VOICE` | Twilio text-to-speech voice for IVR prompts. Defaults to `Polly.Aditi` (Indian English). |

Load them when starting the server:

```bash
python3 -m uvicorn backend.main:app --reload --port 8001 --env-file backend/.env
```

Gemini replies are additionally guarded: the response is rejected if it exceeds a length cap or contains
false claims (e.g. "I contacted someone for you", or promises of confidentiality).

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

# Backend: auth, access control, validation, concurrency, persistence (43 checks, no extra deps)
python3 tests/test_api.py

# Frontend: full browser click-through of all three roles (23 checks)
npm install playwright && npx playwright install chromium
node tests/e2e.mjs
```

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

- **Identity is demo-scale.** Users and sessions live in MySQL (so logins survive a restart) and
  passwords are salted scrypt hashes, but there is no sign-up, password reset, or MFA — this is real
  credential checking and real authorization, not a production identity system.
- **Rate limits are per-process and in-memory.** They reset on restart and are not shared across
  workers; a real deployment would keep the counters in Redis.
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
- **The frontend is a single file** compiled in-browser via Babel — fine for a prototype, but it should move
  to a real build step before it grows further.

### Safety commitments in the current build

- Urgent detection never depends on an LLM or on the ML model
- The system never claims to have contacted anyone on a user's behalf
- The chatbot does not diagnose, prescribe, give definitive legal advice, or promise confidentiality
- High/Critical assessments are explicitly flagged as requiring human review
- Staff can override any AI assessment, and the override is logged
