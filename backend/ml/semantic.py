"""Optional multilingual semantic classifier for non-emergency chat intents.

Safety routing remains regex-based and runs first. This model only improves the
meaning-based classification of non-urgent Hindi, Hinglish, and English messages.
"""
from __future__ import annotations

import os
from functools import lru_cache

INTENTS = {
    "privacy": ["Who can see my chat?", "mera data kaun dekh sakta hai"],
    "support": ["I need to speak with a counsellor", "mujhe kisi se baat karni hai"],
    "legal": ["I need help with my court hearing", "mujhe vakil aur FIR ke baare mein madad chahiye"],
    "practical": ["I need food, housing, or money", "mujhe paise aur rehne ki jagah chahiye"],
    "sleep": ["I cannot sleep and have nightmares", "mujhe neend nahi aa rahi aur bure sapne aate hain"],
    "anxiety": ["I feel anxious, scared, and worried", "mujhe bahut tension aur ghabrahat ho rahi hai"],
    "distress": ["I feel sad, lonely, and exhausted", "main bahut dukhi aur akeli feel kar rahi hoon"],
    "general": ["I want to share something", "mujhe baat karni hai"],
}
EMOTIONS = {
    "fear": ["I am scared and afraid", "mujhe dar lag raha hai"],
    "sadness": ["I feel sad and lonely", "main dukhi aur akeli hoon"],
    "anger": ["I am angry and frustrated", "mujhe bahut gussa aa raha hai"],
    "stress": ["I feel stressed and overwhelmed", "mujhe tension ho rahi hai"],
    "neutral": ["I am okay", "main theek hoon"],
}


@lru_cache(maxsize=1)
def _model():
    # Optional model download must never delay a support conversation.
    if os.getenv("NLP_SEMANTIC_ENABLED", "0") != "1":
        return None
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(os.getenv("NLP_EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"))


@lru_cache(maxsize=1)
def _examples():
    labels, phrases = [], []
    for label, values in INTENTS.items():
        labels.extend(("intent", label) for _ in values); phrases.extend(values)
    for label, values in EMOTIONS.items():
        labels.extend(("emotion", label) for _ in values); phrases.extend(values)
    model = _model()
    return labels, model.encode(phrases, normalize_embeddings=True)


def classify(text: str) -> dict:
    """Return semantic labels, or an empty result if the optional model is unavailable."""
    try:
        model = _model()
        if not model: return {}
        labels, examples = _examples()
        similarities = model.encode([text], normalize_embeddings=True) @ examples.T
        best = {}
        for (kind, label), score in zip(labels, similarities[0]):
            if score > best.get(kind, ("", -1))[1]: best[kind] = (label, float(score))
        return {kind: label for kind, (label, score) in best.items() if score >= .42} | {
            "semantic_confidence": round(best.get("intent", ("", 0))[1] * 100)
        }
    except Exception:
        return {}
