# Raahat Backend

Full documentation — setup, architecture, API reference, and the testing walkthrough — lives in the
[project README](../README.md). This file is a quick backend-only reference.

## Run

```bash
# from the project root
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
python3 -m backend.ml.train
python3 -m uvicorn backend.main:app --reload --port 8001 --env-file backend/.env
```

Interactive API docs: http://127.0.0.1:8001/docs

## Modules

| File | Responsibility |
|---|---|
| `main.py` | FastAPI routes, request validation, per-route authorization |
| `auth.py` | Demo login, in-memory bearer sessions, `require_user` / `require_staff` dependencies |
| `database.py` | MySQL schema, in-place migrations, all queries |
| `ml/generate.py` | Synthetic training data (English, Hindi, Hinglish) |
| `ml/train.py` | Trains text-risk, distress-score, escalation and intervention models |
| `ml/predict.py` | Shared inference and explanation output |
| `services/chatbot.py` | Deterministic multilingual safety routing; optional Gemini replies for non-urgent messages |
| `services/notify.py` | Escalation notifications (console + optional `ALERT_WEBHOOK_URL`) |

Tables: `cases`, `events`, `alerts`, `interventions`, `case_notes`. Migrations run on startup and upgrade
existing databases in place.

## Each module self-tests

```bash
python3 -m backend.services.chatbot   # safety routing assertions (English/Hindi/Hinglish + negation)
python3 -m backend.ml.predict         # inference smoke test
python3 -m backend.ml.generate        # regenerate synthetic dataset
```

Full suites: `python3 tests/test_api.py` and `node tests/e2e.mjs` (see the project README).

## Notes

- Demo accounts: `victim@demo.in`, `counsellor@demo.in`, `admin@demo.in` — all password `demo123`.
- Sessions are in-memory; restarting the server signs everyone out.
- Urgent safety detection is deterministic and never delegated to the LLM or the ML model.

The models and data are demo-only. High and critical assessments require human review; this is not a
clinical, legal, or emergency-dispatch system.
