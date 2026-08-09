"""Round-trip tests for workout_formatter — guards against regressions in
the AI-paste output format that Whoop's parser relies on (Rule 4: whoop_text_v2
default uses ALL-CAPS keywords + 'X OF Y' framing).
"""

from workout_formatter import dedupe, group_exercises, _render_whoop_text_v2


def _row(date, exercise, set_n, weight=40, reps=10, volume_kg=None):
    vol = volume_kg if volume_kg is not None else weight * reps
    return {
        "date": date,
        "exercise": exercise,
        "set_n": set_n,
        "weight_kg": weight,
        "reps": reps,
        "volume_kg": vol,
    }


def test_dedupe_keeps_first_occurrence():
    rows = [
        _row("2026-07-20", "BB BENCH", 1),
        _row("2026-07-20", "BB BENCH", 1),  # exact dup
        _row("2026-07-20", "BB BENCH", 2),
    ]
    out = dedupe(rows)
    assert len(out) == 2
    assert out[0]["set_n"] == 1
    assert out[1]["set_n"] == 2


def test_group_exercises_splits_on_set_n_reset():
    rows = [
        _row("2026-07-20", "BB BENCH", 1),
        _row("2026-07-20", "BB BENCH", 2),
        _row("2026-07-20", "BB BENCH", 3),
        _row("2026-07-20", "ROW", 1),
        _row("2026-07-20", "ROW", 2),
    ]
    groups = group_exercises(rows)
    assert len(groups) == 2
    assert groups[0]["name"] == "BB BENCH"
    assert len(groups[0]["sets"]) == 3
    assert groups[1]["name"] == "ROW"
    assert len(groups[1]["sets"]) == 2


def test_render_whoop_text_v2_uses_all_caps_keywords():
    rows = [
        _row("2026-07-20", "BB BENCH", 1, weight=40, reps=10),
        _row("2026-07-20", "BB BENCH", 2, weight=40, reps=10),
    ]
    out = _render_whoop_text_v2(rows)
    assert "WORKOUT LOG 2026-07-20" in out
    assert "TOTAL EXERCISES: 1" in out
    assert "TOTAL SETS: 2" in out
    assert "EXERCISE 1 OF 1: BB BENCH" in out
    assert "SET 1 OF 2 FOR THIS EXERCISE: 40 kg x 10 reps" in out
    assert "SET 2 OF 2 FOR THIS EXERCISE: 40 kg x 10 reps" in out
    assert "END WORKOUT LOG" in out


def test_render_whoop_text_v2_dedupes_before_rendering():
    """Whoop AI paste crashes on duplicate rows; dedupe must run first."""
    rows = [
        _row("2026-07-20", "BB BENCH", 1),
        _row("2026-07-20", "BB BENCH", 1),
        _row("2026-07-20", "BB BENCH", 1),
    ]
    out = _render_whoop_text_v2(rows)
    assert "TOTAL SETS: 1" in out
    assert out.count("SET 1 OF 1 FOR THIS EXERCISE") == 1


def test_render_handles_bw_weight():
    rows = [_row("2026-07-20", "PULL UP", 1, weight=0, reps=5)]
    out = _render_whoop_text_v2(rows)
    assert "BW x 5 reps" in out