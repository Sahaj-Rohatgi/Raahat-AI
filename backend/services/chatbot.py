"""English support router with optional Gemini replies.

Safety routing is local and deterministic. Gemini is only used for non-urgent
support replies when GEMINI_API_KEY is present in the server environment.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from base64 import b64encode
from urllib import error, request

from ..ml.semantic import classify as semantic_classify


def _has(pattern: str, text: str) -> bool:
    return re.search(pattern, text) is not None


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower().replace("\u2019", "'")
    for old, new in {
        "can't": "cannot",
        "won't": "will not",
        "don't": "do not",
        "doesn't": "does not",
        "i'm": "i am",
        "i've": "i have",
    }.items():
        text = text.replace(old, new)
    return " ".join(text.split())


# Python's \b relies on \w/\W transitions, and Devanagari dependent vowel signs (matras) are not
# treated as \w — so a plain \b(?:...)\b wrapper silently fails to match most Hindi words ending in
# a matra. Use whitespace-based boundaries instead, which work uniformly for English/Hindi/Hinglish.
_START, _END = r"(?:^|(?<=[\s.!?,;:\u0964]))", r"(?:$|(?=[\s.!?,;:\u0964]))"


def _bounded(pattern: str) -> str:
    return _START + r"(?:" + pattern + r")" + _END


SAFETY = {
    # Each pattern carries an English branch plus Hindi (Devanagari) and Hinglish (romanized) branches,
    # since the deterministic safety net must not depend on the English-only ML text model.
    "medical_emergency": _bounded(r"overdos\w*|too many pills|swallowed (?:bleach|poison)|cannot breathe|chest pain|unconscious|not breathing|severe bleeding|heart attack|stroke|seizure|ज़हर खा लिया|जहर खा लिया|साँस नहीं आ रही|सांस नहीं आ रही|दिल का दौरा|बेहोश हो गई|बेहोश हो गया|zeher kha liya|zahar kha liya|jyada dawai kha li|sans nahi aa rahi|saans nahi aa rahi|dil ka daura|behosh ho gaya|behosh ho gayi"),
    "self_harm": _bounded(r"suicid\w*|self[- ]harm\w*|kill myself|hurt myself|harm myself|cut myself|end my life|want to die|do not want to live|no reason to live|wish i (?:was|were) dead|better off dead|आत्महत्या\w*|खुद को नुकसान|खुद को मार\w*|जान देना|जान दे दूं\w*|मरना चाहता हूं|मरना चाहती हूं|जीने का मन नहीं|khud ko maarna chahti hoon|khud ko maarna chahta hoon|khud ko nuksan|khudkushi\w*|aatmahatya\w*|marna chahta hoon|marna chahti hoon|jeena nahi chahta|jeena nahi chahti|jaan de doon\w*|jaan dena chahti hoon|jaan dena chahta hoon"),
    "violence": _bounded(r"(?:he|she|they).{0,45}(?:kill|stab|shoot|attack|hurt|hit|beat|chok|strangl)\w* me|(?:is|are) (?:hitting|beating|attacking|choking|strangling) me|(?:kill|stab|shoot|attack|hurt) (?:him|her|them|someone)|मुझे मार रहा है|मुझे मार रही है|मुझे पीट रहा है|मुझे पीट रही है|वो मुझे मार रहा है|mujhe maar raha hai|mujhe maar rahi hai|mujhe peet raha hai|mujhe peet rahi hai|mujhe maarne wala hai|mujhe gala ghot raha hai"),
    "child_safeguarding": _bounded(r"child abuse|child sexual abuse|(?:child|kid|minor|daughter|son|\d+[- ]?year[- ]?old).{0,70}(?:abus\w*|hurt|unsafe|touch\w*|molest\w*|sexual|hit|beaten)|बच्च\w*.{0,40}(?:छू रहा|छू रही|नुकसान|पीट रहा|पीट रही|असुरक्षित)|bachche ko chhoo raha|bachche ko chhoo rahi|bachche ko maar raha|bachche ko maar rahi|bachcha surakshit nahi hai"),
    "threats": _bounded(r"threat\w*|intimidat\w*|stalk\w*|blackmail\w*|outside my (?:door|house)|following me|has a (?:gun|knife|weapon)|धमकी\w*|जान से मारने की धमकी|dhamki\w*|jaan se marne ki dhamki|jaan se maarne ki dhamki|ghar ke bahar khada hai"),
    "abuse": _bounded(r"abus\w*|rape[ds]?|sexual assault|domestic violence|molest\w*|forced me|hit(?:s|ting)? me|beat(?:s|ing)? me|strangl\w*|controlling me|बलात्कार\w*|यौन शोषण|घरेलू हिंसा|मुझे मारता है|मुझे मारती है|पति.{0,15}मारता है|पत्नी.{0,15}मारती है|balatkar\w*|yaun shoshan|ghar mein maarpeet|mera pati mujhe maarta hai|meri patni mujhe maarti hai|mujhe roz peeta jata hai|mujhe rozana peeta jata hai"),
}
# Elevated but not necessarily an emergency. SAFETY covers danger to the user or a child *now*;
# these patterns cover witnessing violence and acute trauma symptoms, which the SAFETY regexes
# missed entirely: "violence" only matches active-voice perpetrator phrasing ("he is hitting me",
# "kill someone"), so a passive report — "i saw someone getting killed in front of me" — routed to
# "general" and was scored Low. Witnessing a killing is not a 112-call-right-now situation on its
# own, so it does not claim urgent=True; it routes to its own topic and carries a score floor so
# the case cannot read as Low. A witnessed event WITH an immediacy marker ("right now", "outside",
# "weapon") is still escalated to the urgent violence path below.
ELEVATED = {
    "trauma": (
        _bounded(
            r"(?:saw|seen|witness\w*|watched).{0,50}(?:kill\w*|murder\w*|stab\w*|shot|shoot\w*|died|dying|dead|beaten|attack\w*)|"
            r"(?:someone|somebody|a person|a man|a woman|a child|a body).{0,25}(?:getting|being|was|were)\s+(?:kill\w*|murder\w*|stab\w*|shot|attack\w*|beaten)|"
            r"ptsd|p\.t\.s\.d\.?|post[- ]traumatic\w*|flash ?back\w*|panic attack\w*|"
            r"(?:trigger(?:s|ed|ing)?)\s+(?:my|me|her|his|their)|(?:my|her|his|their)\s+\w+\s+(?:is|are)\s+trigger\w*|"
            r"मेरी आँखों के सामने.{0,25}(?:मार|कत्ल|हत्या)|मेरे सामने.{0,25}(?:मार|कत्ल|हत्या)|"
            r"किसी को मरते देखा|लाश देखी|सदमा लग\w*|पुरानी याद\w* लौट\w*|घबराहट का दौरा|"
            r"mere saamne.{0,25}(?:maar|katl|hatya)|meri aankhon ke saamne.{0,25}(?:maar|katl|hatya)|"
            r"kisi ko marte dekha|laash dekhi|sadma lag\w*|panic ka daura|purani yaad\w* laut\w*"
        )
    ),
}
# Score floor per elevated topic, applied in main._chat_payload. Below the urgent floor (90),
# above the Low band so a witnessed killing cannot leave the case reading "Low Risk".
ELEVATED_FLOOR = {"trauma": 68}
# Deliberately narrower than IMMEDIATE, which is used to qualify an already-urgent report. A bare
# "now" is far too weak to escalate a trauma disclosure: in "i saw someone getting killed ... and
# now its triggering my ptsd", the "now" belongs to the symptom, not to ongoing violence, and
# IMMEDIATE turned an aftermath account into a false Critical alert. Only unambiguous
# still-happening markers escalate here.
ONGOING = r"\b(?:right now|happening now|is happening|still going on|outside (?:my |the )?(?:door|house|window)?|has a (?:gun|knife|weapon)|with a (?:gun|knife|weapon)|in danger|call (?:the )?(?:police|ambulance|112|100))\b"
TOPICS = {
    "privacy": r"\b(?:privacy|private|confidential|who can read|who will read|share my (?:data|messages)|delete my (?:data|messages|account)|mera data|kaun padh|delete karo)\b",
    "support": r"\b(?:talk to (?:a )?(?:counsellor|counselor|human|person)|need someone to talk to|connect me|support staff|need help|kisi se baat|counsellor se baat|madad chahiye)\b",
    "legal": r"\b(?:legal|lawyer|court|hearing|fir|police report|complaint|witness|rights|compensation|vakil|sunwai|shikayat)\b",
    "practical": r"\b(?:rent|housing|shelter|food|money|job|transport|paperwork|paise|rehne ki jagah|kaam)\b",
    "sleep": r"\b(?:sleep\w*|insomnia|nightmares?|awake|neend|bure sapne)\b",
    "anxiety": r"\b(?:anxious|anxiety|panic\w*|worried|afraid|stress\w*|unsafe|ghabrahat|tension|dar lag)\b",
    "distress": r"\b(?:sad|lonely|hopeless|depressed|crying|worthless|exhausted|very low|dukhi|akela|ro rahi|thak gayi)\b",
}
NEGATION = r"\b(?:not|never|no longer|do not|does not|will not|without|denies|deny)\s+(?:\w+\s+){0,4}"
IMMEDIATE = r"\b(?:now|right now|tonight|today|outside|weapon|gun|knife|not safe|in danger)\b"
ACKNOWLEDGEMENT = r"(?:yes|yeah|yep|haan|ha|ji|ok(?:ay)?|please|help|madad)[.!?\u0964]?"

EMERGENCY = {
    "medical_emergency": "This could be a medical emergency. Contact local emergency services now or ask someone nearby to call and stay with you.",
    "self_harm": "Contact local emergency services or a crisis service now, and ask someone you trust to stay with you. Move away from anything you could use to hurt yourself if you can do so safely.",
    "violence": "Someone may be in immediate danger. Create distance if you can do so safely, and contact local emergency services now.",
    "child_safeguarding": "A child may be in immediate danger. Contact local emergency services and seek a safe adult who is not involved in the harm.",
    "threats": "If someone is threatening you nearby, avoid confrontation and move to a safer place if you can. Contact local emergency services now.",
    "abuse": "What happened is not your fault. Move away from the person harming you if you can safely and contact local emergency services or a trusted person nearby.",
}
REPLIES = {
    "trauma": "Seeing something like that can stay with you long after it happens, and the reactions you are having are a normal response to it. You are not in trouble for telling me. Would it help to talk about what is happening in your body right now, or about what you saw?",
    "privacy": "Chats and check-ins are stored for approved care-team review in this demo. Avoid sharing unnecessary identifying details; what privacy concern should we focus on?",
    "support": "You can request human support through the app, but I cannot contact anyone from this chat. What would you like support staff to help with?",
    "legal": "I can help organize questions, but I cannot determine your legal rights or predict a case outcome. Is your question about a hearing, complaint, or compensation?",
    "practical": "Which practical problem needs attention first: food, housing, money, travel, or paperwork?",
    "sleep": "Is it harder to fall asleep, stay asleep, or cope with nightmares? We can focus on the part that is bothering you most.",
    "anxiety": "What is worrying you most right now? If you feel unsafe, tell me whether the danger is nearby or more of a general feeling.",
    "distress": "That sounds difficult to carry. What has been weighing on you most today?",
    "general": "Thank you for sharing. What would feel most helpful to talk through right now?",
}
HINGLISH_REPLIES = {
    "trauma": "Aisa kuch dekhna baad tak asar karta hai, aur jo reactions aapko ho rahe hain wo uska normal response hai. Abhi aapke body mein kya ho raha hai, ya jo aapne dekha uske baare mein — kis par baat karna theek lagega?",
    "privacy": "Is demo mein chats aur check-ins approved care team review ke liye store hote hain. Aapko privacy ke baare mein kis baat ki chinta hai?",
    "support": "Main aapki baat sunne ke liye yahan hoon. Main chat se kisi ko contact nahi kar sakta, lekin care team support request dekh sakti hai. Aapko kis cheez mein madad chahiye?",
    "legal": "Main questions organize karne mein madad kar sakta hoon, lekin legal advice ya case outcome nahi bata sakta. Aapka sawal hearing, complaint ya compensation ke baare mein hai?",
    "practical": "Sabse pehle kis practical problem par baat karein: food, housing, paise, travel, ya paperwork?",
    "sleep": "Neend aane mein dikkat hai, beech mein neend khul jaati hai, ya bure sapne aate hain? Jo sabse zyada pareshan kar raha hai usse shuru karte hain.",
    "anxiety": "Abhi sabse zyada kis baat ki tension ya ghabrahat ho rahi hai? Agar aap unsafe feel kar rahe hain, bataiye danger paas mein hai ya general feeling hai.",
    "distress": "Yeh bahut mushkil lag raha hai. Aaj sabse zyada kis baat ka bojh feel ho raha hai?",
    "general": "Apni baat share karne ke liye thank you. Abhi kis cheez par baat karna aapke liye helpful hoga?",
}

SYSTEM = """You are Raahat, a trauma-informed support assistant.
Reply in 2-4 short sentences with at most one useful question. Do not diagnose,
prescribe, give definitive legal advice, promise confidentiality, or claim you
contacted anyone. Do not ask for identifying details, make up resources, or repeat
questions already answered in the conversation. The chat is stored for approved
care-team review. Match the user's language: Hinglish for Roman Hindi/Hinglish, Hindi
for Devanagari Hindi, English otherwise."""
FALSE_CLAIM = r"\b(?:i|we).{0,20}(?:contacted|notified|called|dispatched|deleted|booked)\b|\b(?:confidential|secret|only between us)\b"
AUDIO_TYPES = {"audio/webm", "audio/ogg", "audio/wav", "audio/mpeg", "audio/mp4"}


def _negated(text: str, start: int) -> bool:
    # Negation only applies within its clause; "not suicidal, but want to die" is urgent.
    clause = re.split(r"[.!?,;:\u0964]|\b(?:but|however|and)\b", text[:start])[-1]
    return _has(NEGATION + r"$", clause)


def route(message: str, history: list[dict] | None = None) -> dict:
    text = _normalize(message)
    for label, pattern in SAFETY.items():
        matches = list(re.finditer(pattern, text))
        if any(not _negated(text, match.start()) for match in matches):
            return {"urgent": True, "topic": label}
    unsafe = re.search(r"\bunsafe\b", text)
    if unsafe and _has(IMMEDIATE, text) and not _negated(text, unsafe.start()):
        return {"urgent": True, "topic": "threats"}
    for label, pattern in ELEVATED.items():
        matches = list(re.finditer(pattern, text))
        if any(not _negated(text, match.start()) for match in matches):
            # Witnessed violence described as still happening is an emergency, not an aftermath.
            if _has(ONGOING, text):
                return {"urgent": True, "topic": "violence", "score_floor": 90}
            return {"urgent": False, "topic": label, "score_floor": ELEVATED_FLOOR[label]}
    semantic = semantic_classify(message)
    if semantic.get("intent") and semantic["intent"] != "general":
        return {"urgent": False, "topic": semantic["intent"], "semantic_confidence": semantic.get("semantic_confidence", 0)}
    for topic, pattern in TOPICS.items():
        if _has(pattern, text):
            return {"urgent": False, "topic": topic}
    # Keep an active crisis in context for short acknowledgements such as "yes" or "haan".
    for turn in reversed(history or []):
        if turn.get("role") == "user":
            previous = route(str(turn.get("content", "")), [])
            if previous["urgent"] and _has(r"^" + ACKNOWLEDGEMENT + r"$", text):
                return previous
            if previous["topic"] != "general":
                return {"urgent": False, "topic": previous["topic"]}
            break
    return {"urgent": False, "topic": "general", "semantic_confidence": semantic.get("semantic_confidence", 0)}


def language(message: str) -> str:
    if re.search(r"[\u0900-\u097f]", message):
        return "hindi"
    if _has(r"\b(?:mujhe|mera|meri|mujh|hai|hain|nahi|kyu|kyun|kya|aap|main|mein|bahut|thoda|tension|ghabrahat|neend|madad|baat)\b", _normalize(message)):
        return "hinglish"
    return "english"


def emotion(message: str) -> str:
    text = _normalize(message)
    for label, pattern in {
        "fear": r"\b(?:afraid|fear|panic|worried|unsafe|dar|ghabrahat)\b",
        "sadness": r"\b(?:sad|lonely|hopeless|depressed|crying|dukhi|akela)\b",
        "anger": r"\b(?:angry|furious|rage|gussa)\b",
        "stress": r"\b(?:stress|tension|exhausted|overwhelmed|thak)\b",
    }.items():
        if _has(pattern, text):
            return label
    return "neutral"


def _local_reply(routed: dict, response_language: str) -> str:
    if routed["urgent"]:
        if response_language in ("hinglish", "hindi"):
            return "Yeh urgent situation ho sakti hai. Agar safe ho toh kisi trusted person ke paas jaiye aur local emergency services ko abhi contact kijiye. Main aapki taraf se kisi ko contact nahi kar sakta. Kya aapke paas koi safe person hai?"
        return EMERGENCY[routed["topic"]] + " I cannot contact anyone on your behalf. Is someone safe nearby who can help now?"
    if response_language == "hinglish":
        return HINGLISH_REPLIES.get(routed["topic"], HINGLISH_REPLIES["general"])
    return REPLIES.get(routed["topic"], REPLIES["general"])


def _gemini_reply(message: str, history: list[dict], routed: dict, assessment: dict | None) -> str | None:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").removeprefix("models/")
    if not key or routed["urgent"] or not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        return None
    contents = [
        {"role": "model" if turn.get("role") == "assistant" else "user", "parts": [{"text": str(turn.get("content", ""))}]}
        for turn in history[-12:]
        if str(turn.get("content", "")).strip()
    ]
    contents.append({"role": "user", "parts": [{"text": message}]})
    context = json.dumps({"topic": routed["topic"], "risk_band": (assessment or {}).get("risk_band")})
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM + "\nContext: " + context}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.35, "maxOutputTokens": 220},
    }
    try:
        req = request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "x-goog-api-key": key},
            method="POST",
        )
        with request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read(500_000))
        reply_text = " ".join(
            part["text"]
            for part in data["candidates"][0]["content"]["parts"]
            if isinstance(part, dict) and "text" in part and not part.get("thought")
        ).strip()
        if reply_text and len(reply_text) <= 1200 and not _has(FALSE_CLAIM, _normalize(reply_text)):
            return reply_text
    except (OSError, error.URLError, KeyError, IndexError, TypeError, ValueError):
        return None
    return None


def transcribe_audio(audio: bytes, mime_type: str, history: list[dict] | None = None) -> str | None:
    """Return a transcript only; urgency remains the local text router's job."""
    key = os.getenv("GEMINI_API_KEY", "").strip()
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").removeprefix("models/")
    if not key or mime_type not in AUDIO_TYPES or not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        return None
    contents = [
        {"role": "model" if turn.get("role") == "assistant" else "user", "parts": [{"text": str(turn.get("content", ""))}]}
        for turn in (history or [])[-12:]
        if str(turn.get("content", "")).strip()
    ]
    contents.append({"role": "user", "parts": [
        {"text": "Transcribe this voice note accurately. Return only the spoken words; do not infer emotion, risk, or facts."},
        {"inlineData": {"mimeType": mime_type, "data": b64encode(audio).decode()}},
    ]})
    payload = {"systemInstruction": {"parts": [{"text": "You transcribe voice notes for a support service. Preserve Hindi, Hinglish, and English words."}]},
               "contents": contents, "generationConfig": {"temperature": 0, "maxOutputTokens": 800}}
    try:
        req = request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "x-goog-api-key": key}, method="POST",
        )
        with request.urlopen(req, timeout=20) as response:
            data = json.loads(response.read(500_000))
        transcript = " ".join(part["text"] for part in data["candidates"][0]["content"]["parts"]
                                if isinstance(part, dict) and "text" in part and not part.get("thought")).strip()
        return transcript[:4000] or None
    except (OSError, error.URLError, KeyError, IndexError, TypeError, ValueError):
        return None


def reply(message: str, history: list[dict] | None = None, assessment: dict | None = None) -> dict:
    history = history or []
    routed = route(message, history)
    semantic = semantic_classify(message)
    gemini = _gemini_reply(message, history, routed, assessment)
    response_language = language(message)
    return {"reply": gemini or _local_reply(routed, response_language), "urgent": routed["urgent"],
            "topic": routed["topic"], "intent": routed["topic"], "emotion": semantic.get("emotion", emotion(message)),
            "score_floor": routed.get("score_floor", 0),
            "language": response_language, "handoff": routed["urgent"] or routed["topic"] in ("support", "trauma"),
            "semantic_confidence": semantic.get("semantic_confidence", routed.get("semantic_confidence", 0)), "provider": "Gemini" if gemini else "Local safe-response"}


if __name__ == "__main__":
    assert reply("I want to hurt myself")["urgent"]
    assert reply("I want to die.")["urgent"]
    assert reply("I do not want to live anymore")["urgent"]
    assert not reply("I am not suicidal, just stressed")["urgent"]
    assert reply("I am not suicidal and want to die.")["urgent"]
    assert reply("I do not want to die, but I want to hurt myself.")["urgent"]
    assert reply("He is hitting me right now")["urgent"]
    assert reply("He is hitting me.")["urgent"]
    assert reply("My 12 year old daughter is being touched")["urgent"]
    assert reply("My partner hits me every night")["urgent"]
    assert reply("Who can read my messages?")["topic"] == "privacy"
    assert reply("I need someone to talk to")["topic"] == "support"
    assert reply("mujhe maar raha hai")["urgent"]
    assert reply("mujhe maar raha hai.")["urgent"]
    assert reply("मेरा पति मुझे मारता है")["urgent"]
    assert reply("मुझे मार रहा है।")["urgent"]
    assert reply("khud ko nuksan pahunchana chahti hoon")["urgent"]
    assert reply("वो घर के बाहर धमकी दे रहा है")["urgent"]
    witnessed = reply("i think i saw someone getting killed in frount of me and now its triggering my ptsd.")
    assert witnessed["topic"] == "trauma" and not witnessed["urgent"] and witnessed["score_floor"] == 68
    assert reply("someone is being stabbed outside right now")["urgent"]
    assert reply("mere saamne usko maar diya")["topic"] == "trauma"
    assert reply("i keep getting flashbacks")["topic"] == "trauma"
    assert reply("I feel calm and supported today")["topic"] != "trauma"
