"""Deterministic risk-term floor for the text feature.

The TF-IDF text classifier is trained on a small synthetic vocabulary (~680 terms). A
message built from words it has never seen collapses to stopwords, and the classifier
then returns its prior — "Stable" — which maps to the LOWEST text_risk value. In a
safety product that is the wrong direction: a message the model cannot read was
actively *lowering* the distress score.

This lexicon is the deterministic layer between the safety regex and the ML model. It
never lowers anything; it sets a FLOOR on text_risk from explicit risk terms, and the
model is free to grade higher. Terms cover English, Hindi (Devanagari) and Hinglish,
because the score model must not be English-only (same reasoning as chatbot.SAFETY).
"""
from __future__ import annotations

import re
import unicodedata

from .generate import RISK_LEVELS

# Whitespace/punctuation boundaries rather than \b: Devanagari dependent vowel signs (matras)
# are not \w, so \b silently fails to match most Hindi words. Same fix as chatbot._bounded.
_START, _END = r"(?:^|(?<=[\s.!?,;:।]))", r"(?:$|(?=[\s.!?,;:।]))"

# Each entry maps a floor band to the terms that justify it. The floor is the highest
# band whose pattern matches anywhere in the message.
FLOOR_TERMS = {
    "Critical": r"kill\w*|murder\w*|stab\w*|shot dead|shoot\w*|strangl\w*|suffocat\w*|rap(?:e|ed|ing)|molest\w*|overdos\w*|dead body|bleeding|bled|corpse|"
                r"मार डाला|कत्ल|हत्या|खून|लाश|बलात्कार\w*|गोली मार\w*|"
                r"maar dala|maar diya|katl|hatya|laash|balatkar\w*|goli maar\w*|khoon",
    "High": r"ptsd|p\.t\.s\.d|post[- ]traumatic|flash ?back\w*|panic attack\w*|trauma\w*|trigger(?:ed|ing|s)?|"
            r"witness\w*|violence|violent|assault\w*|weapon|gun|knife|blade|threat\w*|terrified|petrified|"
            r"unsafe|not safe|in danger|nightmare\w*|cannot cope|can not cope|breaking down|"
            r"सदमा|धमकी\w*|हिंसा|हमला|चाकू|बंदूक|असुरक्षित|खतरे में|बुरे सपने|डरी हुई|डरा हुआ|"
            r"sadma|dhamki\w*|hinsa|hamla|chaku|bandook|asurakshit|khatre mein|bure sapne|dari hui|dara hua",
    "Moderate": r"anxious|anxiety|depress\w*|hopeless|worthless|crying|cried|exhaust\w*|overwhelm\w*|"
                r"alone|lonely|scared|afraid|stressed|insomnia|cannot sleep|can not sleep|shaking|numb|"
                r"चिंता|घबराहट|अकेल\w*|उदास|दुखी|तनाव|नींद नहीं|रो रही|रो रहा|डर लग\w*|"
                r"chinta|ghabrahat|akela|akeli|akelapan|udaas|dukhi|tanaav|tension|neend nahi|ro rahi|ro raha|dar lag\w*",
}
# Compiled once, ordered most severe first so the first match wins.
_PATTERNS = [
    (RISK_LEVELS.index(band), re.compile(_START + r"(?:" + terms + r")" + _END))
    for band, terms in FLOOR_TERMS.items()
]


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).lower().replace("’", "'").split())


def risk_floor(text: str) -> int:
    """Lowest text_risk index this message may be scored at, as an index into RISK_LEVELS.

    Returns 0 (no floor) when the message contains no explicit risk term.
    """
    if not (text or "").strip():
        return 0
    normalized = _normalize(text)
    for index, pattern in _PATTERNS:
        if pattern.search(normalized):
            return index
    return 0


if __name__ == "__main__":
    assert risk_floor("i saw someone getting killed in frount of me") == RISK_LEVELS.index("Critical")
    assert risk_floor("its triggering my ptsd") == RISK_LEVELS.index("High")
    assert risk_floor("मुझे बुरे सपने आते हैं") == RISK_LEVELS.index("High")
    assert risk_floor("mujhe bahut ghabrahat ho rahi hai") == RISK_LEVELS.index("Moderate")
    assert risk_floor("I feel calm and supported today") == 0
    assert risk_floor("") == 0
    print("lexicon ok")
