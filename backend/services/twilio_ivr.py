"""Twilio Programmable Voice IVR — the same phone-in flow as the FreeSWITCH dialplan,
driven by Twilio webhooks instead, so a real phone number can reach it.

Twilio is the opposite shape to FreeSWITCH: instead of our server receiving the call,
Twilio's servers call *us* over HTTP for each step and we answer with TwiML (XML) saying
what to do next. That means the backend must be publicly reachable (ngrok or a deploy).

Neither transport carries business logic: both end up in the same assess/record/sos
pipeline in main.py that the web check-in and chat already use.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from xml.sax.saxutils import escape, quoteattr

# Polly.Aditi is Twilio's Indian-English voice. Any Twilio voice name works here; the
# plain default is used if this is set to an empty string.
VOICE = os.getenv("TWILIO_VOICE", "Polly.Aditi")
LANGUAGE = os.getenv("TWILIO_VOICE_LANGUAGE", "en-IN")

MENU_PROMPT = "Welcome to Raahat. For a wellbeing check in, press 1. For emergency help, press 9."
NO_INPUT = "We did not receive any input. Goodbye."
INVALID = "Sorry, that was not a valid option. Goodbye."
SOS_DONE = ("Your emergency alert has been sent to the care team immediately. "
            "If you are in danger, please try to reach a safe place or contact local emergency services. Goodbye.")
CHECKIN_DONE = ("Thank you. Your check in has been recorded, and a member of the care team "
                "will follow up if needed. Goodbye.")

# (field, prompt, highest valid digit) — the same five answers the web check-in collects,
# so a phone check-in scores identically to one submitted in the app.
# Digit direction matters: for every field, a HIGHER digit means MORE distress, matching the
# web check-in's option order and the scale the models were trained on. The mood and sleep
# prompts used to run the other way ("0 for very low, up to 3 for good"), so a caller in
# distress pressing 0 was scored as the healthiest possible — inverting the risk score on the
# one entry path used by people without a smartphone.
QUESTIONS = [
    ("mood", "How is your mood today? Press 0 for good, 1 for okay, 2 for low, or 3 for struggling.", 3),
    ("sleep", "How has your sleep been? Press 0 for restful, 1 for a little disturbed, 2 for poor, or 3 for very poor.", 3),
    ("safety", "Do you feel safe right now? Press 0 for safe, 1 for somewhat unsafe, or 2 for unsafe.", 2),
    ("anxiety", "How anxious do you feel? Press 0 for calm, up to 2 for very anxious.", 2),
    ("case_pressure", "How much pressure do you feel from your case right now? Press 0 for none, up to 3 for a lot.", 3),
]


def validate_signature(signature: str, url: str, params: dict[str, str]) -> bool:
    """Twilio signs every webhook: base64 HMAC-SHA1 of the full public URL plus each POST
    parameter (sorted by name, key and value concatenated), keyed by the account auth token.

    With no TWILIO_AUTH_TOKEN configured there is nothing to verify against, so this returns
    True — fine for local curl testing, but set the token before pointing a real number here.
    """
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    if not token:
        return True
    payload = url + "".join(name + params[name] for name in sorted(params))
    digest = hmac.new(token.encode(), payload.encode("utf-8"), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature or "")


def _say(text: str) -> str:
    voice = f" voice={quoteattr(VOICE)} language={quoteattr(LANGUAGE)}" if VOICE else ""
    return f"<Say{voice}>{escape(text)}</Say>"


def gather(prompt: str, action: str) -> str:
    """Ask for a single keypad digit, POSTing it to `action`. If the caller says nothing,
    the <Gather> falls through to the lines after it and the call ends politely."""
    return ('<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Gather numDigits="1" timeout="10" method="POST" action={quoteattr(action)}>{_say(prompt)}</Gather>'
            f"{_say(NO_INPUT)}<Hangup/></Response>")


def say_and_hangup(text: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{_say(text)}<Hangup/></Response>'
