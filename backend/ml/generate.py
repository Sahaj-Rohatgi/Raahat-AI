"""Create broad synthetic examples (English, Hindi, and Hinglish) for the demo models."""
from __future__ import annotations

import csv
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_PATH = ROOT / "data" / "synthetic_cases.csv"
# Each band mixes English, Hindi (Devanagari), and Hinglish (romanized Hindi) phrasing so the text
# classifier isn't only tuned to English — the deterministic safety net in chatbot.py already covers
# urgent messages in all three, but the *score* model needs multilingual examples of its own to grade
# non-urgent Hindi/Hinglish check-ins correctly instead of guessing from English-only training data.
RISK_TEXT = {
    "Stable": [
        "I feel calm and supported today", "sleep has been restful and I feel safe",
        "my hearing went okay and I am hopeful", "my counsellor session helped and I am managing",
        "मैं आज शांत और सुरक्षित महसूस कर रही हूं", "नींद अच्छी रही और मैं ठीक हूं",
        "सुनवाई ठीक रही और मुझे उम्मीद है", "मेरी काउंसलिंग सेशन से मदद मिली",
        "main aaj shant aur surakshit mehsoos kar rahi hoon", "neend achi rahi aur main theek hoon",
        "sunwai theek rahi aur mujhe umeed hai", "counselling session se madad mili",
    ],
    "Low": [
        "I feel a little worried about paperwork", "sleep was slightly disturbed but I am managing",
        "I need help with rent documents", "I feel nervous but I have support from my family",
        "मुझे कागजी कार्रवाई को लेकर थोड़ी चिंता है", "नींद थोड़ी खराब थी लेकिन मैं संभाल रही हूं",
        "मुझे किराए के दस्तावेज़ों में मदद चाहिए", "मुझे घबराहट है लेकिन परिवार का साथ है",
        "mujhe paperwork ko lekar thodi tension hai", "neend thodi kharab thi lekin main sambhal rahi hoon",
        "mujhe rent ke documents mein madad chahiye", "mujhe ghabrahat hai lekin family ka saath hai",
    ],
    "Moderate": [
        "I feel anxious before the court hearing", "I have nightmares and feel stressed",
        "I am struggling with money and legal pressure", "I feel alone after the investigation update",
        "अदालत की सुनवाई से पहले मुझे चिंता होती है", "मुझे बुरे सपने आते हैं और तनाव महसूस होता है",
        "मैं पैसे और कानूनी दबाव से जूझ रही हूं", "जांच अपडेट के बाद मुझे अकेलापन महसूस होता है",
        "adalat ki sunwai se pehle mujhe chinta hoti hai", "mujhe bure sapne aate hain aur tanaav mehsoos hota hai",
        "main paise aur legal pressure se joojh rahi hoon", "investigation update ke baad akelapan mehsoos hota hai",
    ],
    "High": [
        "I feel unsafe and afraid they may find me", "I have not slept and feel hopeless",
        "I am being threatened to withdraw my complaint", "I need relocation because I do not feel safe at home",
        "मुझे लगता है कि वे मुझे ढूंढ लेंगे और मैं असुरक्षित हूं", "मैं सो नहीं पाई और निराश महसूस कर रही हूं",
        "मुझे शिकायत वापस लेने की धमकी दी जा रही है", "मुझे घर में सुरक्षित नहीं लगता, मुझे स्थानांतरण चाहिए",
        "mujhe lagta hai wo mujhe dhoond lenge aur main asurakshit hoon", "main so nahi payi aur nirash mehsoos kar rahi hoon",
        "mujhe complaint wapas lene ki dhamki di ja rahi hai", "ghar mein surakshit nahi lagta, mujhe relocation chahiye",
    ],
    "Critical": [
        "I want to hurt myself and I am not safe", "he is outside with a knife and I am scared",
        "I took too many pills and need help now", "my child is being hurt and we need urgent help",
        "मैं खुद को नुकसान पहुंचाना चाहती हूं और सुरक्षित नहीं हूं", "वह चाकू लेकर बाहर खड़ा है और मैं डरी हुई हूं",
        "मैंने बहुत सारी गोलियां खा लीं और मुझे अभी मदद चाहिए", "मेरे बच्चे को नुकसान पहुंचाया जा रहा है, हमें तुरंत मदद चाहिए",
        "main khud ko nuksan pahunchana chahti hoon aur surakshit nahi hoon", "wo chaku leke bahar khada hai aur main dari hui hoon",
        "maine bahut sari goliyan kha li hain aur mujhe abhi madad chahiye", "mere bachche ko nuksan pahunchaya ja raha hai, hamein turant madad chahiye",
    ],
}
# The single source of truth for how many missed check-in cycles the pipeline can represent.
# database.missed_checkins() clamps to this, predict.FEATURE_RANGES normalizes against it, and
# the rows below must span it: training data that stopped at 5 left the forest unable to tell
# 6 missed cycles from 10, silently flattening the strongest disengagement signal in the system.
MAX_MISSED_CHECKINS = 10
# The one canonical severity ordering. RISK_TEXT is declared in this order, and both training
# and inference must encode the text band against it: scikit-learn's classes_ is sorted
# ALPHABETICALLY (Critical, High, Low, Moderate, Stable), so using its index as the ordinal
# text_risk feature fed the models a scrambled severity scale — "Stable" encoded as the highest
# value and "Critical" as the lowest, with Low/Moderate/High out of order in between.
RISK_LEVELS = list(RISK_TEXT)
SUPPORTS = ["Counselling", "Medical Support", "Witness Protection", "Legal Assistance", "Financial Assistance", "Relocation Support", "Rehabilitation Support"]
_MONEY_WORDS = ("money", "पैसे", "पैसा", "paise", "paisa")


def build(rows: int = 1800, seed: int = 42) -> Path:
    random.seed(seed)
    DATA_PATH.parent.mkdir(exist_ok=True)
    records = []
    BANDS = list(RISK_TEXT)
    WEIGHTS = [28, 25, 23, 16, 8]
    for _ in range(rows):
        # `level` is how severe the check-in ANSWERS are; `text_level` is how severe the MESSAGE
        # is. They used to be the same number for every row, so the models only ever saw the two
        # agreeing — and a real request where they disagree (a chat message with no check-in
        # behind it, or a check-in from someone who writes nothing) fell outside the training
        # distribution entirely, producing non-monotonic scores. A third of rows now disagree.
        level = random.choices(range(5), WEIGHTS)[0]
        text_level = level if random.random() > .35 else random.choices(range(5), WEIGHTS)[0]
        band = BANDS[text_level]
        # Not every assessment has a message behind it: a guided check-in is answers only. Those
        # rows are generated explicitly (has_message=0, and the text term drops out of the score)
        # so the models learn that "no message" means the text feature carries no information —
        # rather than having to read a missing message as a maximally calm one.
        has_message = int(random.random() > .3)
        mood = min(3, max(0, level + random.choice([-1, 0, 0, 1])))
        sleep = min(3, max(0, level + random.choice([-1, 0, 1])))
        safety = min(2, max(0, (level - 1) // 2 + random.choice([0, 0, 1])))
        anxiety = min(2, max(0, level // 2 + random.choice([0, 1])))
        case_pressure = min(3, max(0, level + random.choice([-1, 0, 1])))
        missed = max(0, min(MAX_MISSED_CHECKINS, level * 2 + random.choice([-2, -1, 0, 0, 1, 2])))
        engagement = max(0, min(4, 4 - level + random.choice([-1, 0, 1])))
        # text_risk (the band the text model assigns to the message) is a term in the score, not
        # just a correlated feature. Without it the forest learned a score that depended only on
        # the check-in answers, so a chat message the text model graded High moved the distress
        # score by ~4 points — the product's central claim, reading distress from what someone
        # writes, never reached the score. A message with no check-in behind it now scores on its
        # own; a check-in with no message contributes 0 here, exactly as before.
        score = max(0, min(100, int(8 + mood * 12 + sleep * 8 + safety * 15 + anxiety * 8 + case_pressure * 7
                                    + missed * 5 - engagement * 3 + (text_level * 8 if has_message else 0)
                                    + random.gauss(0, 4))))
        severity = max(level, text_level) if has_message else level
        escalates = int(severity >= 3 or (severity >= 2 and missed >= 3 and safety >= 1))
        # Draw the row's text ONCE. It used to be sampled separately here and again when the
        # record was built, so the "Financial Assistance" label below was decided from a
        # sentence that never appeared in the row — pure label noise for that class.
        text = random.choice(RISK_TEXT[band])  # risk_band label below is this text's own band
        requested = []
        if level >= 1: requested.append("Counselling")
        if sleep >= 3: requested.append("Medical Support")
        if safety >= 1: requested.append("Witness Protection")
        if case_pressure >= 2: requested.append("Legal Assistance")
        if any(word in text for word in _MONEY_WORDS): requested.append("Financial Assistance")
        if severity >= 3 and safety >= 1: requested.append("Relocation Support")
        if severity >= 2: requested.append("Rehabilitation Support")
        records.append({"text": text, "risk_band": band, "score": score, "escalates": escalates, "has_message": has_message,
                        "mood": mood, "sleep": sleep, "safety": safety, "anxiety": anxiety, "case_pressure": case_pressure,
                        "missed_checkins": missed, "engagement": engagement, "interventions": "|".join(sorted(set(requested)))})
    with DATA_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=records[0])
        writer.writeheader(); writer.writerows(records)
    return DATA_PATH


if __name__ == "__main__":
    assert build().exists()
    print(DATA_PATH)
