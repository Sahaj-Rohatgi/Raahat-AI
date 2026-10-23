"""Short-horizon distress forecast: where a case's score is heading, not where it is.

The assessment score in ml.predict.assess() is a *current* reading — it answers "how is this
person right now". A counsellor triaging a caseload also needs the other question: which of
these people is likely to be worse at the next check-in, while there is still time to act.

This module answers that from the case's own recorded assessment history and nothing else. It
is deliberately a transparent, inspectable projection (a time-weighted least-squares trend on
the stored scores, shrunk toward "no change" by how well that trend actually fits) rather than
a second trained model: there is no longitudinal training corpus in this project, so a model
claiming to predict the future here would be fiction.

Three properties matter more than sophistication here, and each was a real defect first:

* **Time is real, not ordinal.** Events do not arrive on a tidy cadence — the seeded case has
  five events inside thirteen seconds and then a four-hour gap. Fitting against the event
  *index* projected twelve readings that spanned four hours as if they spanned twelve weeks.
  The fit runs against elapsed days, and a history that does not span MIN_SPAN_DAYS gets no
  projection at all.
* **Only readings that measure a level are trend points.** An SOS writes a constant 100 (an
  alarm, not a measurement) and a counsellor override writes escalation_probability 0 as a
  placeholder. Feeding either into a trend or into the escalation adjustment invented movement
  that nobody observed. See TREND_KINDS.
* **It must never be worse than assuming no change.** Backtested against a naive "the score
  stays where it is" baseline, an unshrunk trend beat naive on sustained trends but lost badly
  on noisy or step-change histories — exactly the histories a distressed case produces. The
  slope is therefore scaled by how much of the variance the trend actually explains, so a
  history the line does not fit degrades to the naive baseline instead of amplifying noise.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .predict import band_for

# Event kinds whose score is a reading of the person's current *level*, and so a legitimate
# point on a trend line. Excluded: "sos" (a constant 100 alarm flag — a real and important
# event, but not a measurement of a level) and anything else added later, which has to be
# opted in here deliberately rather than silently joining the trend.
TREND_KINDS = ("checkin", "chat", "override")
# A trend needs at least three readings: two points always fit a straight line perfectly and
# would report a confident slope from what may be a single noisy check-in.
MIN_POINTS = 3
# ...and those readings have to span real time. Below this, the history describes a moment,
# not a trajectory, and extrapolating it two weeks out is not a forecast.
MIN_SPAN_DAYS = 3.0
# How far ahead the projection runs, in days.
HORIZON_DAYS = 14
# Only readings inside this window are fitted — a case's trajectory six months ago says little
# about the fortnight ahead, and keeping old readings in the fit is what made a case that
# stepped up and then stabilised keep projecting upward. WINDOW_MIN_POINTS overrides the window
# when a case checks in rarely, so a slow-moving case still gets a projection.
WINDOW_DAYS = 42.0
WINDOW_MIN_POINTS = 5
# Recency half-life inside that window: a reading this many days old counts half as much as one
# taken today, so a case that has recently turned around is not dragged by older scores.
HALF_LIFE_DAYS = 21.0
# Cap on how many readings are fitted, newest first.
MAX_POINTS = 20
# How many of the most recent readings are re-fitted on their own as a check on the window
# trend — see the `agreement` factor in forecast().
RECENT_CHECK_POINTS = 3
# A latest reading this far from the fitted trend level is called out — it is the difference
# between "this case is trending up" and "this one reading was unusually high".
OFF_TREND_POINTS = 6

# Deliberately absent: an adjustment from the latest assessment's escalation_probability. An
# earlier version nudged the projection by it, which was wrong twice over — it is a reading of
# *current* risk rather than a rate of change, and there is no way to validate the adjustment
# against the backtest, so it was an unmeasured term moving a measured number. It could also
# flip the displayed direction against the trend the same card was reporting. The escalation
# probability is surfaced on its own in the assessment panel, where it means what it says.
# A projected move smaller than this is reported as "steady" rather than as a direction — the
# scores are integers on a 0-100 scale and a 2-point drift is noise, not a trajectory.
STEADY_BAND = 4


def _parse(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _fit(days: list[float], scores: list[float], weights: list[float]) -> tuple[float, float, float, float]:
    """Weighted least-squares fit of score against elapsed days.

    Returns (slope per day, residual spread, R², standard error of the slope). Weights are
    normalized to average 1 so the residual variance is on the scale of the scores and the
    standard error is the usual one, which is what the shrinkage in forecast() needs.
    """
    count = len(scores)
    scale = count / sum(weights)
    weights = [w * scale for w in weights]
    total = sum(weights)
    mean_x = sum(w * x for w, x in zip(weights, days)) / total
    mean_y = sum(w * y for w, y in zip(weights, scores)) / total
    variance = sum(w * (x - mean_x) ** 2 for w, x in zip(weights, days))
    covariance = sum(w * (x - mean_x) * (y - mean_y) for w, x, y in zip(weights, days, scores))
    slope = covariance / variance if variance else 0.0
    intercept = mean_y - slope * mean_x
    residual = sum(w * (y - (intercept + slope * x)) ** 2 for w, x, y in zip(weights, days, scores))
    spread_total = sum(w * (y - mean_y) ** 2 for w, y in zip(weights, scores))
    r_squared = max(0.0, 1 - residual / spread_total) if spread_total > 1e-9 else 0.0
    # Residual variance carries (count - 2) degrees of freedom: a straight line spends two.
    sigma_squared = residual / max(1, count - 2)
    standard_error = (sigma_squared / variance) ** .5 if variance > 1e-9 else float("inf")
    return slope, (residual / total) ** .5, r_squared, standard_error


def _unavailable(reason: str, points: int) -> dict:
    return {"available": False, "reason": reason, "points_used": points}


def forecast(points, horizon_days: int = HORIZON_DAYS, cadence_days: int = 7) -> dict:
    """Project this case's distress score `horizon_days` ahead from its recorded history.

    `points` is oldest-first, either (timestamp, score) pairs or bare scores — bare scores are
    treated as one check-in cadence apart, which is what a caller with no timestamps is
    implicitly assuming anyway.
    """
    prepared: list[tuple[datetime | None, float]] = []
    for point in points:
        if isinstance(point, (tuple, list)) and len(point) == 2:
            prepared.append((_parse(point[0]), float(point[1])))
        else:
            prepared.append((None, float(point)))

    if len(prepared) < MIN_POINTS:
        return _unavailable(f"Needs at least {MIN_POINTS} recorded assessments to project a trend "
                            f"({len(prepared)} so far).", len(prepared))

    # Synthesize a cadence-spaced timeline only when the caller supplied no timestamps at all.
    if all(stamp is None for stamp, _ in prepared):
        now = datetime.now(timezone.utc)
        days = [-(len(prepared) - 1 - index) * float(cadence_days) for index in range(len(prepared))]
        latest_at = now
    else:
        stamped = [(stamp, score) for stamp, score in prepared if stamp is not None]
        if len(stamped) < MIN_POINTS:
            return _unavailable(f"Only {len(stamped)} of this case's assessments carry a usable "
                                f"timestamp — not enough to project a trend.", len(stamped))
        prepared = stamped
        latest_at = max(stamp for stamp, _ in prepared)
        days = [(stamp - latest_at).total_seconds() / 86400 for stamp, _ in prepared]

    scores = [score for _, score in prepared]
    span = max(days) - min(days)
    if span < MIN_SPAN_DAYS:
        return _unavailable(f"This case's {len(scores)} assessments span only "
                            f"{_span_label(span)} — too short a window to project a "
                            f"{horizon_days}-day trend from.", len(scores))

    # Restrict the fit to the recent window (see WINDOW_DAYS), keeping at least
    # WINDOW_MIN_POINTS readings so a case that checks in rarely is still projected.
    windowed = [index for index, day in enumerate(days) if day >= -WINDOW_DAYS]
    if len(windowed) < min(WINDOW_MIN_POINTS, len(days)):
        windowed = list(range(max(0, len(days) - WINDOW_MIN_POINTS), len(days)))
    # The cap on how many readings are fitted is applied *after* the time window, not before.
    # Capping first meant a burst of events in a single afternoon could evict every reading
    # from the preceding weeks, leaving a history that spanned hours and no projection at all.
    windowed = windowed[-MAX_POINTS:]
    latest_score = scores[-1]
    days = [days[index] for index in windowed]
    scores = [scores[index] for index in windowed]
    span = max(days) - min(days)
    if len(scores) < MIN_POINTS or span < MIN_SPAN_DAYS:
        return _unavailable(f"This case's recent assessments span only {_span_label(max(span, 0))} — "
                            f"too short a window to project a {horizon_days}-day trend from.", len(scores))

    weights = [.5 ** (-day / HALF_LIFE_DAYS) for day in days]
    slope, spread, r_squared, standard_error = _fit(days, scores, weights)

    # Shrinkage — the property the backtest is built around, and the one that decides whether
    # this module is worth shipping at all. The slope is scaled by how much of it is signal
    # rather than estimation noise: slope² / (slope² + se²) is ~1 when the trend is large
    # against its own standard error and ~0 when it is not, so a scattered or step-shaped
    # history collapses toward "no change" instead of extrapolating noise two weeks out.
    # Shrinking by R² alone was not enough — R² stays moderate on a noisy trend while the
    # slope estimate is still far too uncertain to project from, and the backtest showed the
    # forecast losing to the naive baseline on exactly those histories.
    signal = slope ** 2 / (slope ** 2 + standard_error ** 2) if standard_error not in (0, float("inf")) else (
        1.0 if standard_error == 0 else 0.0)
    # `reach` refuses to extrapolate further beyond the observed window than that window
    # supports. R² and the signal ratio catch different failures — R² catches a history the
    # line does not describe, the signal ratio catches a slope too small to distinguish from
    # its own estimation error — so both are applied.
    reach = min(1.0, span / horizon_days)

    # Level-shift guard. A case that jumped once and has been stable since still produces a
    # strong linear fit across the window — a straight line describes a step reasonably well —
    # and the projection kept climbing from a level the case had already settled at. Re-fitting
    # just the most recent readings catches this: if they have gone flat, the window slope is no
    # longer live and is scaled down toward it. This can only ever shrink the slope (a case
    # that is accelerating is capped at the window trend, never amplified beyond it).
    agreement = 1.0
    if len(scores) > RECENT_CHECK_POINTS and abs(slope) > 1e-9:
        recent_days, recent_scores = days[-RECENT_CHECK_POINTS:], scores[-RECENT_CHECK_POINTS:]
        if max(recent_days) - min(recent_days) >= 1:
            recent_slope, *_ = _fit(recent_days, recent_scores, [1.0] * RECENT_CHECK_POINTS)
            # Opposite signs mean the recent readings contradict the window trend outright.
            agreement = 0.0 if recent_slope * slope <= 0 else min(1.0, abs(recent_slope / slope))

    reliability = r_squared * signal * reach * agreement

    # The projection is anchored on the fitted trend level, not on the single latest reading.
    # Anchoring on the latest reading imported that one reading's noise into every projection,
    # which is why the backtest had the forecast losing to the naive baseline on volatile
    # histories. The anchor slides back toward the raw latest reading as reliability falls, so
    # a history with no usable trend still reports "roughly where they are now".
    current = scores[-1]
    fitted_now = _level_at_zero(days, scores, weights, slope)
    baseline = current + (fitted_now - current) * reliability
    projected = baseline + slope * horizon_days * reliability

    projected = max(0, min(100, projected))
    # Movement is measured against the fitted level the projection actually starts from, so the
    # direction, the delta and the per-week trend can never contradict each other on the card.
    delta = projected - baseline
    direction = "steady" if abs(delta) < STEADY_BAND else ("rising" if delta > 0 else "easing")
    off_trend = current - fitted_now if abs(current - fitted_now) >= OFF_TREND_POINTS else 0

    # Confidence describes the fit, not the person. It falls with scatter around the trend
    # line and with a history too short or too shallow to extrapolate from.
    confidence = round(max(30, min(90, (max(0.0, 1 - spread / 18) * .5 + reliability * .5) * 100)))
    margin = round(min(30, max(4, spread * 1.2 + (1 - reliability) * 8 + horizon_days / 7 * 2)))

    return {
        "available": True,
        "score": round(projected),
        "risk_band": band_for(projected),
        "current_score": round(current),
        "delta": round(delta),
        "direction": direction,
        "confidence": confidence,
        "range": [max(0, round(projected) - margin), min(100, round(projected) + margin)],
        "horizon_days": horizon_days,
        "horizon_label": f"next {horizon_days} days",
        "points_used": len(scores),
        "span_days": round(span, 1),
        "trend_level_now": round(baseline),
        "off_trend_note": ("" if not off_trend else
                           f"The latest reading ({round(current)}) sits {abs(round(off_trend))} points "
                           f"{'above' if off_trend > 0 else 'below'} this case's fitted trend, so the "
                           f"projection starts from the trend rather than from that single reading."),
        "trend_per_week": round(slope * reliability * 7, 1),
        "fit_quality": round(r_squared * 100),
        "basis": (f"Time-weighted trend across {len(scores)} recorded assessments spanning "
                  f"{_span_label(span)}, projected {horizon_days} days out at "
                  f"{round(reliability * 100)}% strength — the trend is scaled down by how well it "
                  f"actually fits this case's readings, so a scattered history stays near today's level."),
    }


def _level_at_zero(days: list[float], scores: list[float], weights: list[float], slope: float) -> float:
    """The fitted trend level at "now" (day 0) — a noise-averaged estimate of where this case
    currently is, as opposed to whatever the single most recent reading happened to be."""
    total = sum(weights)
    mean_x = sum(w * x for w, x in zip(weights, days)) / total
    mean_y = sum(w * y for w, y in zip(weights, scores)) / total
    return mean_y - slope * mean_x


def _span_label(days: float) -> str:
    if days < 1: return f"{round(days * 24)} hours"
    if days < 14: return f"{round(days)} days"
    return f"{round(days / 7)} weeks"


def forecast_from_events(events: list[dict], cadence_days: int = 7,
                         horizon_days: int = HORIZON_DAYS) -> dict:
    """Take this case's event rows (any order, each carrying a decoded `assessment`) and
    forecast from the ones that actually measure a distress level — see TREND_KINDS."""
    usable = []
    for event in events:
        assessment = event.get("assessment") or {}
        if not isinstance(assessment, dict) or "score" not in assessment: continue
        if event.get("kind") not in TREND_KINDS: continue
        usable.append((event.get("created_at") or "", event.get("kind"), assessment))
    usable.sort(key=lambda item: item[0])
    if len(usable) < MIN_POINTS:
        return _unavailable(f"Needs at least {MIN_POINTS} recorded assessments to project a trend "
                            f"({len(usable)} so far).", len(usable))

    return forecast([(stamp, assessment["score"]) for stamp, _, assessment in usable],
                    horizon_days, cadence_days)


if __name__ == "__main__":
    from datetime import timedelta
    base = datetime.now(timezone.utc)
    rising = forecast([(base - timedelta(days=28 - 7 * i), score) for i, score in enumerate([30, 42, 55, 67])])
    assert rising["available"] and rising["direction"] == "rising", rising
    assert not forecast([40, 45])["available"]
    # A burst of readings inside one afternoon describes a moment, not a trajectory.
    burst = forecast([(base - timedelta(minutes=m), s) for m, s in [(12, 30), (8, 60), (4, 90)]])
    assert not burst["available"], burst
    assert rising["delta"] > 0 and rising["trend_per_week"] > 0, rising
    print(rising["basis"])
    print(burst["reason"])
