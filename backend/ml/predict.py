"""Inference adapter shared by check-ins and chatbot messages."""
from __future__ import annotations

import statistics
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import joblib

from .generate import MAX_MISSED_CHECKINS, RISK_LEVELS, SUPPORTS
from .lexicon import risk_floor
from .train import ARTIFACTS, FEATURES, train

BANDS = RISK_LEVELS  # single source of truth for the severity ordering (see ml/generate.py)
ARTIFACT_PATH = ARTIFACTS / "distress_models.joblib"

# The text classifier is trained on a small synthetic vocabulary, so a message written in words
# it has never seen reduces to stopwords and the model returns its prior — "Stable" — with a
# confidence barely above chance. Grading such a message as the CALMEST possible one meant an
# unreadable message pushed the distress score *down*: "i saw someone getting killed in frount of
# me and now its triggering my ptsd" matched only {and, i, me, my, now} and scored 30/Low. Below
# either threshold the model is treated as having no opinion and the text feature takes a neutral
# value, so the score rests on the answers and on the deterministic lexicon floor instead.
MIN_VOCAB_COVERAGE = .35
MIN_TEXT_CONFIDENCE = .40
NEUTRAL_TEXT_RISK = RISK_LEVELS.index("Moderate")

# Max plausible value per feature, used only to normalize each raw value onto a comparable
# 0-1 scale before weighting it by the trained model's feature_importances_. This is a coarse,
# instance-level *approximation* of "what drove this score" (importance x normalized value) —
# not a SHAP/LIME decomposition — but it is derived from the real trained model and the actual
# answers given, unlike the illustrative placeholder bars this replaced in the UI.
FEATURE_RANGES = {"mood": 3, "sleep": 3, "safety": 2, "anxiety": 2, "case_pressure": 3,
                   "missed_checkins": MAX_MISSED_CHECKINS, "engagement": 4, "has_message": 1, "text_risk": 4}
FACTOR_LABELS = {"mood": "Low mood", "sleep": "Sleep disruption", "safety": "Safety concern",
                  "anxiety": "Anxiety signal", "case_pressure": "Case-related pressure",
                  "missed_checkins": "Missed check-ins", "engagement": "Low engagement",
                  "text_risk": "Message risk language"}


@lru_cache(maxsize=8)
def _load(mtime: float):
    """Keyed on the artifact's mtime so a retrain is picked up without restarting the server.
    An unkeyed cache meant the process kept serving the model it loaded first, and a retrained
    artifact on disk was silently ignored until someone restarted uvicorn."""
    return joblib.load(ARTIFACT_PATH)


def _models():
    if not ARTIFACT_PATH.exists(): train()
    return _load(ARTIFACT_PATH.stat().st_mtime)


def model_version() -> str:
    """A stable identifier for the currently-loaded model artifact, so stored assessments
    can be traced back to the model that produced them (e.g. after a retrain)."""
    _models()  # ensures the artifact exists
    mtime = datetime.fromtimestamp(ARTIFACT_PATH.stat().st_mtime, tz=timezone.utc)
    return mtime.isoformat()


def model_metrics() -> dict:
    """Cross-validated metrics stored with the artifact by ml.train.evaluate()."""
    return _models().get("metrics", {})


def band_for(score: float) -> str:
    """The single mapping from a 0-100 score to a risk band, shared by assess() and callers
    that raise a score after the fact (e.g. the chat safety floor in main._chat_payload)."""
    return BANDS[min(4, max(0, round(score / 25)))]


def _vocab_coverage(vectorizer, message: str) -> float:
    """Share of the message's unigrams the vectorizer has actually seen in training."""
    tokens = [token for token in vectorizer.build_analyzer()(message) if " " not in token]
    if not tokens: return 0.0
    return sum(token in vectorizer.vocabulary_ for token in tokens) / len(tokens)


def assess(text: str, answers: dict | None = None, previous_score: int = 0, missed_checkins: int = 0,
           engagement: int | None = None) -> dict:
    models, answers = _models(), answers or {}
    # The text model is trained on natural-language messages only. A structured check-in has no
    # message, and feeding it the generated summary ("...mood=0, sleep=0, safety=0...") pushed
    # that out-of-distribution string through the classifier, which graded it Moderate and
    # injected a phantom text_risk into EVERY check-in — a participant answering "everything is
    # fine" scored Moderate instead of Stable. With no message there is no risk language to
    # observe, so the text feature takes its lowest value and the score comes from the answers.
    message = (text or "").strip()
    if message:
        vector = models["vectorizer"].transform([message])
        probabilities = models["text"].predict_proba(vector)[0]
        confidence, coverage = float(probabilities.max()), _vocab_coverage(models["vectorizer"], message)
        if coverage < MIN_VOCAB_COVERAGE or confidence < MIN_TEXT_CONFIDENCE:
            text_band, text_risk = "Uncertain", NEUTRAL_TEXT_RISK
            text_reason = (f"Text model could not read this message ({round(coverage * 100)}% of its words are in the "
                           f"model's vocabulary, top class {round(confidence * 100)}%) — scored neutrally, not as calm")
        else:
            text_band = str(models["text"].classes_[probabilities.argmax()])
            text_risk = RISK_LEVELS.index(text_band)
            text_reason = f"Text model classified the message as {text_band} ({round(confidence * 100)}% confidence)"
        # Deterministic floor: explicit risk terms outrank whatever the small text model inferred.
        floor = risk_floor(message)
        if floor > text_risk:
            text_reason += f"; explicit risk language sets a {RISK_LEVELS[floor]} floor"
            text_band, text_risk = RISK_LEVELS[floor], floor
    else:
        text_band, text_risk = "No message", 0
        text_reason = "No message text — scored from the check-in answers alone"
    has_message = 1 if message else 0
    values = {"mood": 1, "sleep": 1, "safety": 0, "anxiety": 1, "case_pressure": 1, "engagement": 3}
    values.update({key: int(value) for key, value in answers.items() if key in values and isinstance(value, (int, float))})
    values["missed_checkins"] = missed_checkins
    # Engagement comes from the case's real recent activity (database.engagement_level); the
    # default above only applies to a caller that has no case context, such as a self-test.
    if engagement is not None: values["engagement"] = max(0, min(4, int(engagement)))
    row = [[values[name] for name in FEATURES[:-2]] + [has_message, text_risk]]
    score_model = models["score"]
    score = round(float(score_model.predict(row)[0]))
    escalation = round(float(models["escalation"].predict_proba(row)[0][1]) * 100)
    selected = [name for name, model in zip(SUPPORTS, models["intervention"].estimators_) if model.predict_proba(row)[0][1] >= .45]
    if not selected: selected = ["Counselling"]
    band = band_for(score)
    reasons = [text_reason]
    for key, label in (("safety", "safety concern"), ("sleep", "poor sleep"), ("missed_checkins", "missed check-ins"), ("anxiety", "anxiety signal")):
        if values[key] >= (1 if key == "safety" else 2): reasons.append(label)

    # Explainability: weight each feature's normalized value by the trained model's global
    # feature_importances_ to approximate its contribution to *this* prediction, then scale
    # the weights so the largest contributor reads as 100 (a relative "how much did this matter").
    raw_row = row[0]
    contributions = []
    for name, importance, value in zip(FEATURES, score_model.feature_importances_, raw_row):
        # Every feature except engagement pushes risk *up* as it grows. Engagement is the
        # opposite — staying in contact is protective — so its contribution is weighted by
        # how far it falls short of the maximum. Without this inversion the panel labelled a
        # highly engaged case's top risk driver "Low engagement".
        if name == "has_message": continue  # a modelling flag, not a risk factor to display
        normalized = value / FEATURE_RANGES[name]
        if name == "engagement": normalized = 1 - normalized
        weight = importance * normalized
        if weight > 0: contributions.append((name, weight, value))
    contributions.sort(key=lambda item: item[1], reverse=True)
    top_weight = contributions[0][1] if contributions else 0
    factors = [
        {"key": name, "label": FACTOR_LABELS[name], "value": value,
         "weight": round((weight / top_weight) * 100) if top_weight else 0}
        for name, weight, value in contributions[:5]
    ]

    # Confidence: agreement across the forest's individual trees for this row — a tight spread
    # of tree predictions means the model is confident in this score, a wide spread means it isn't.
    tree_predictions = [tree.predict(row)[0] for tree in score_model.estimators_]
    spread = statistics.pstdev(tree_predictions) if len(tree_predictions) > 1 else 0
    confidence = round(max(40, min(99, 100 - spread * 2.2)))

    return {"score": max(0, min(100, score)), "risk_band": band, "escalation_probability": escalation,
            "interventions": selected, "explanations": reasons, "factors": factors, "confidence": confidence,
            "text_band": text_band, "previous_score": previous_score}


if __name__ == "__main__":
    result = assess("I feel unsafe and have not slept", {"sleep": 3, "safety": 2, "anxiety": 2}, engagement=1)
    assert result["score"] >= 50 and result["interventions"]
    print(result)
