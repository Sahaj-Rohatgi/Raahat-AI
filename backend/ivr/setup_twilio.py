"""Point a Twilio number at this server's IVR webhook.

Twilio only calls us over HTTP, so the number's "A call comes in" URL has to name a
publicly reachable address. That URL changes every time a free ngrok tunnel restarts,
which makes doing this by hand in the console a chore — hence this script.

It reads the account credentials from backend/.env, discovers the running ngrok tunnel,
and updates the number in place. Stdlib only: the two REST calls below are the whole of
the Twilio API surface this project uses, so the SDK would not earn its dependency.

    python3 -m backend.ivr.setup_twilio              # update every number on the account
    python3 -m backend.ivr.setup_twilio +919812345678
    python3 -m backend.ivr.setup_twilio --url https://example.com   # skip ngrok discovery
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.twilio.com/2010-04-01"
NGROK_API = "http://127.0.0.1:4040/api/tunnels"
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_env() -> None:
    """Read backend/.env without a dotenv dependency; real environment variables win."""
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def request(method: str, url: str, sid: str, token: str, fields: dict[str, str] | None = None):
    data = urllib.parse.urlencode(fields).encode() if fields else None
    credentials = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Basic {credentials}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        if exc.code == 401:
            sys.exit("Twilio rejected the credentials (401). Check TWILIO_ACCOUNT_SID and "
                     "TWILIO_AUTH_TOKEN in backend/.env against console.twilio.com.")
        sys.exit(f"Twilio API error {exc.code}: {detail}")


def public_url() -> str:
    """Ask the local ngrok agent for the HTTPS URL it is currently serving."""
    try:
        with urllib.request.urlopen(NGROK_API, timeout=5) as response:
            tunnels = json.load(response)["tunnels"]
    except OSError:
        sys.exit("No ngrok agent found on :4040. Start one with `ngrok http 8001`, "
                 "or pass --url https://your-public-host explicitly.")
    for tunnel in tunnels:
        if tunnel.get("public_url", "").startswith("https://"):
            return tunnel["public_url"]
    sys.exit("ngrok is running but has no HTTPS tunnel; Twilio webhooks must be HTTPS.")


def main(argv: list[str]) -> None:
    load_env()
    sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    if not sid or not token:
        sys.exit("Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in backend/.env "
                 "(both are on the console.twilio.com dashboard).")

    override, wanted = None, []
    rest = list(argv)
    while rest:
        arg = rest.pop(0)
        if arg == "--url":
            if not rest:
                sys.exit("--url needs a value, e.g. --url https://example.com")
            override = rest.pop(0)
        else:
            wanted.append(arg)

    base = (override or public_url()).rstrip("/")
    voice_url = f"{base}/api/twilio/voice"

    numbers = request("GET", f"{API}/Accounts/{sid}/IncomingPhoneNumbers.json", sid, token)["incoming_phone_numbers"]
    if not numbers:
        sys.exit("This Twilio account has no phone numbers yet. Buy one first: "
                 "console.twilio.com -> Phone Numbers -> Buy a number (needs Voice capability).")
    if wanted:
        numbers = [n for n in numbers if n["phone_number"] in wanted]
        if not numbers:
            sys.exit(f"None of {', '.join(wanted)} are on this account.")

    for number in numbers:
        updated = request("POST", f"{API}/Accounts/{sid}/IncomingPhoneNumbers/{number['sid']}.json",
                          sid, token, {"VoiceUrl": voice_url, "VoiceMethod": "POST"})
        print(f"{updated['phone_number']} -> {updated['voice_url']} ({updated['voice_method']})")

    print(f"\nDone. Dial the number above; the IVR menu is served from {base}.")
    print("Re-run this after any ngrok restart — the free tier issues a new URL each time.")


if __name__ == "__main__":
    main(sys.argv[1:])
