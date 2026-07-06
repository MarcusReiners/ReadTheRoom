"""
Fusionsregeln und Schwellenwerte (ADR-001 Abschnitt 3).

Heuristische Gewichte - siehe ADR-001 Abschnitt 6 (Limitationen):
sollten spaeter empirisch kalibriert werden.
"""

WEIGHT_APPROACH = 0.5
WEIGHT_DIALOG_PATTERN = 0.5
FOCUS_SHIFT_THRESHOLD = 0.6

ROTATION_THRESHOLD_DEGREES = 8.0


def approach_score(distance_mm: float, speed_mm_s: float) -> float:
    """
    Naeherungsscore: geringe Distanz + Annaeherung/Stillstand -> hoher Score.
    Vereinfachtes Modell fuer Phase 1, siehe ADR-001 Abschnitt 3.2.
    """
    distance_component = max(0.0, 1.0 - (distance_mm / 3000.0))
    approach_component = max(0.0, -speed_mm_s / 500.0) if speed_mm_s < 0 else 0.0
    return min(1.0, distance_component * 0.6 + approach_component * 0.4)


def dialog_pattern_score(recent_speaker_ids: list[int]) -> float:
    """
    Erkennt Sprecherwechsel-Muster (ADR-001 Abschnitt 3.3).
    Vereinfachte Heuristik fuer Phase 1: Anteil der Wechsel in der
    letzten Sprechakt-Historie.
    """
    if len(recent_speaker_ids) < 2:
        return 0.0
    switches = sum(
        1 for i in range(1, len(recent_speaker_ids))
        if recent_speaker_ids[i] != recent_speaker_ids[i - 1]
    )
    return min(1.0, switches / (len(recent_speaker_ids) - 1))


def focus_shift_score(distance_mm: float, speed_mm_s: float, recent_speaker_ids: list[int]) -> float:
    a = approach_score(distance_mm, speed_mm_s)
    d = dialog_pattern_score(recent_speaker_ids)
    return WEIGHT_APPROACH * a + WEIGHT_DIALOG_PATTERN * d


def should_trigger_focus_shift(score: float) -> bool:
    return score > FOCUS_SHIFT_THRESHOLD
