from __future__ import annotations

import pytest

from swarm_ledger.rubric import (
    DIMENSION_KEYS,
    DIMENSIONS,
    Rating,
    Thresholds,
    below_floor,
    classify,
    dimension_scores,
    disagreements,
    issues_from,
    passes,
    validate_ratings,
)

EXPECTED_DIMENSION_KEYS = (
    "meets_the_brief",
    "testing",
    "error_handling",
    "security",
    "architecture",
    "code_structure",
    "performance",
    "maintainability",
    "accessibility",
)


def test_dimensions_cover_the_nine_rubric_dimensions_in_order() -> None:
    assert DIMENSION_KEYS == EXPECTED_DIMENSION_KEYS
    for key, title, criteria in DIMENSIONS:
        assert key
        assert title
        assert len(criteria) >= 1
        for criterion_key, criterion_text in criteria:
            assert criterion_key
            assert criterion_text


def test_thresholds_defaults() -> None:
    t = Thresholds()
    assert t.target == 90
    assert t.floor == 70
    assert t.criterion_floor == 5
    assert t.disagreement_gap == 10
    assert t.plateau == 2
    assert t.regression_tolerance == 5


def _full_ratings(dimension: str, value: int = 10, reason: str | None = None) -> list[Rating]:
    criteria = next(criteria for key, _, criteria in DIMENSIONS if key == dimension)
    return [Rating(dimension, criterion_key, value, reason, None) for criterion_key, _ in criteria]


def _all_applicable_ratings(value: int = 10, reason: str | None = None) -> list[Rating]:
    ratings: list[Rating] = []
    for key, _, _ in DIMENSIONS:
        ratings.extend(_full_ratings(key, value, reason))
    return ratings


def _all_applicable() -> dict[str, str | None]:
    return {key: None for key, _, _ in DIMENSIONS}


def test_validate_ratings_accepts_a_complete_valid_set() -> None:
    validate_ratings(_all_applicable_ratings(), _all_applicable())


def test_validate_ratings_rejects_value_below_range() -> None:
    ratings = [Rating("meets_the_brief", "does_what_was_asked", 0, "bad", None)]
    with pytest.raises(ValueError):
        validate_ratings(ratings, {"meets_the_brief": None})


def test_validate_ratings_rejects_value_above_range() -> None:
    ratings = [Rating("meets_the_brief", "does_what_was_asked", 11, "bad", None)]
    with pytest.raises(ValueError):
        validate_ratings(ratings, {"meets_the_brief": None})


def test_validate_ratings_accepts_boundary_values_one_and_ten() -> None:
    ratings = [
        Rating("meets_the_brief", "does_what_was_asked", 1, "wrong", "pkg/good.py:1"),
        Rating("meets_the_brief", "nothing_extra", 10, None, None),
    ]
    validate_ratings(ratings, {"meets_the_brief": None})


def test_validate_ratings_rejects_low_rating_with_no_reason() -> None:
    ratings = [
        Rating("meets_the_brief", "does_what_was_asked", 8, None, None),
        Rating("meets_the_brief", "nothing_extra", 10, None, None),
    ]
    with pytest.raises(ValueError):
        validate_ratings(ratings, {"meets_the_brief": None})


def test_validate_ratings_rejects_low_rating_with_empty_reason() -> None:
    ratings = [
        Rating("meets_the_brief", "does_what_was_asked", 8, "", None),
        Rating("meets_the_brief", "nothing_extra", 10, None, None),
    ]
    with pytest.raises(ValueError):
        validate_ratings(ratings, {"meets_the_brief": None})


def test_validate_ratings_allows_rating_of_nine_with_no_reason() -> None:
    ratings = [
        Rating("meets_the_brief", "does_what_was_asked", 9, None, None),
        Rating("meets_the_brief", "nothing_extra", 9, None, None),
    ]
    validate_ratings(ratings, {"meets_the_brief": None})


def test_validate_ratings_rejects_missing_criterion_for_applicable_dimension() -> None:
    ratings = [Rating("meets_the_brief", "does_what_was_asked", 10, None, None)]
    with pytest.raises(ValueError):
        validate_ratings(ratings, {"meets_the_brief": None})


def test_validate_ratings_rejects_rating_for_not_applicable_dimension() -> None:
    ratings = [Rating("accessibility", "ui_files_only", 10, None, None)]
    with pytest.raises(ValueError):
        validate_ratings(ratings, {"accessibility": "back-end file"})


def test_validate_ratings_allows_not_applicable_dimension_with_no_ratings() -> None:
    ratings = _full_ratings("meets_the_brief")
    validate_ratings(ratings, {"meets_the_brief": None, "accessibility": "back-end file"})


def test_validate_ratings_rejects_unknown_dimension() -> None:
    ratings = [Rating("not_a_real_dimension", "x", 10, None, None)]
    with pytest.raises(ValueError):
        validate_ratings(ratings, {"not_a_real_dimension": None})


def test_validate_ratings_rejects_unknown_criterion() -> None:
    ratings = _full_ratings("meets_the_brief") + [
        Rating("meets_the_brief", "not_a_real_criterion", 10, None, None)
    ]
    with pytest.raises(ValueError):
        validate_ratings(ratings, {"meets_the_brief": None})


def test_validate_ratings_rejects_dimension_missing_from_applicable() -> None:
    ratings = _full_ratings("meets_the_brief")
    with pytest.raises(ValueError):
        validate_ratings(ratings, {})


def test_dimension_scores_computes_mean_times_ten() -> None:
    ratings = [
        Rating("meets_the_brief", "does_what_was_asked", 10, None, None),
        Rating("meets_the_brief", "nothing_extra", 8, "minor", None),
    ]
    scores = dimension_scores(ratings)
    assert scores == {"meets_the_brief": 90.0}


def test_dimension_scores_rounds_to_one_decimal() -> None:
    ratings = [
        Rating("testing", "happy_path", 10, None, None),
        Rating("testing", "edge_cases", 10, None, None),
        Rating("testing", "proves_the_brief", 9, None, None),
    ]
    scores = dimension_scores(ratings)
    assert scores == {"testing": 96.7}


def test_dimension_scores_groups_multiple_dimensions() -> None:
    ratings = [
        Rating("meets_the_brief", "does_what_was_asked", 10, None, None),
        Rating("testing", "happy_path", 8, "minor", None),
    ]
    scores = dimension_scores(ratings)
    assert scores == {"meets_the_brief": 100.0, "testing": 80.0}


def test_passes_is_true_when_every_dimension_meets_target_and_no_criterion_below_floor() -> None:
    t = Thresholds()
    scores = {"meets_the_brief": 90.0, "testing": 95.0}
    ratings = [
        Rating("meets_the_brief", "does_what_was_asked", 9, None, None),
        Rating("testing", "happy_path", 10, None, None),
    ]
    ok, reasons = passes(scores, ratings, t)
    assert ok is True
    assert reasons == []


def test_passes_boundary_score_exactly_at_target_passes() -> None:
    t = Thresholds()
    scores = {"meets_the_brief": 90.0}
    ok, reasons = passes(scores, [], t)
    assert ok is True
    assert reasons == []


def test_passes_fails_when_score_is_one_below_target() -> None:
    t = Thresholds()
    scores = {"meets_the_brief": 89.9}
    ok, reasons = passes(scores, [], t)
    assert ok is False
    assert len(reasons) == 1


def test_passes_boundary_criterion_exactly_at_floor_passes() -> None:
    t = Thresholds()
    scores = {"meets_the_brief": 90.0}
    ratings = [Rating("meets_the_brief", "does_what_was_asked", 5, "issue", None)]
    ok, reasons = passes(scores, ratings, t)
    assert ok is True
    assert reasons == []


def test_passes_fails_when_criterion_is_below_floor() -> None:
    t = Thresholds()
    scores = {"meets_the_brief": 90.0}
    ratings = [Rating("meets_the_brief", "does_what_was_asked", 4, "bad", None)]
    ok, reasons = passes(scores, ratings, t)
    assert ok is False
    assert len(reasons) == 1


def test_passes_collects_multiple_reasons() -> None:
    t = Thresholds()
    scores = {"meets_the_brief": 50.0, "testing": 60.0}
    ratings = [Rating("meets_the_brief", "does_what_was_asked", 2, "bad", None)]
    ok, reasons = passes(scores, ratings, t)
    assert ok is False
    assert len(reasons) == 3


def test_issues_from_includes_values_at_or_below_four() -> None:
    ratings = [
        Rating("meets_the_brief", "does_what_was_asked", 4, "bad", None),
        Rating("meets_the_brief", "nothing_extra", 1, "wrong", None),
    ]
    issues = issues_from(ratings)
    assert issues == ratings


def test_issues_from_excludes_values_above_four() -> None:
    ratings = [
        Rating("meets_the_brief", "does_what_was_asked", 5, "issue", None),
        Rating("meets_the_brief", "nothing_extra", 10, None, None),
    ]
    assert issues_from(ratings) == []


def test_disagreements_empty_when_scores_match() -> None:
    t = Thresholds()
    a = {"meets_the_brief": 90.0}
    b = {"meets_the_brief": 90.0}
    assert disagreements(a, b, t) == []


def test_disagreements_boundary_gap_exactly_at_threshold_counts() -> None:
    t = Thresholds()
    a = {"meets_the_brief": 80.0}
    b = {"meets_the_brief": 90.0}
    assert disagreements(a, b, t) == ["meets_the_brief"]


def test_disagreements_gap_one_below_threshold_and_same_side_of_target_does_not_count() -> None:
    t = Thresholds()
    a = {"meets_the_brief": 90.0}
    b = {"meets_the_brief": 99.0}
    assert disagreements(a, b, t) == []


def test_disagreements_counts_when_scores_straddle_the_target_even_with_small_gap() -> None:
    t = Thresholds()
    a = {"meets_the_brief": 89.0}
    b = {"meets_the_brief": 90.0}
    assert disagreements(a, b, t) == ["meets_the_brief"]


def test_disagreements_skips_dimensions_missing_from_either_side() -> None:
    t = Thresholds()
    a = {"meets_the_brief": 40.0, "testing": 90.0}
    b = {"meets_the_brief": 40.0}
    assert disagreements(a, b, t) == []


def test_disagreements_preserves_dimension_order() -> None:
    t = Thresholds()
    a = {"testing": 40.0, "meets_the_brief": 40.0}
    b = {"testing": 90.0, "meets_the_brief": 90.0}
    assert disagreements(a, b, t) == ["meets_the_brief", "testing"]


def test_classify_regression_when_dimension_falls_by_exactly_the_tolerance() -> None:
    t = Thresholds()
    before = {"meets_the_brief": 90.0}
    after = {"meets_the_brief": 85.0}
    assert classify(before, after, {"meets_the_brief"}, t) == "regression"


def test_classify_no_regression_when_fall_is_one_below_tolerance_and_stays_at_floor() -> None:
    t = Thresholds()
    before = {"meets_the_brief": 90.0}
    after = {"meets_the_brief": 86.0}
    assert classify(before, after, {"meets_the_brief"}, t) == "plateau"


def test_classify_regression_when_a_small_drop_lands_below_the_floor() -> None:
    t = Thresholds()
    before = {"meets_the_brief": 72.0}
    after = {"meets_the_brief": 68.0}
    assert classify(before, after, {"meets_the_brief"}, t) == "regression"


def test_classify_no_regression_when_already_below_floor_and_unchanged() -> None:
    t = Thresholds()
    before = {"meets_the_brief": 60.0, "testing": 60.0}
    after = {"meets_the_brief": 60.0, "testing": 65.0}
    assert classify(before, after, {"testing"}, t) == "improved"


def test_classify_improved_when_targeted_dimension_rises_by_exactly_the_plateau() -> None:
    t = Thresholds()
    before = {"meets_the_brief": 80.0}
    after = {"meets_the_brief": 82.0}
    assert classify(before, after, {"meets_the_brief"}, t) == "improved"


def test_classify_plateau_when_targeted_dimension_rises_by_less_than_plateau() -> None:
    t = Thresholds()
    before = {"meets_the_brief": 80.0}
    after = {"meets_the_brief": 81.0}
    assert classify(before, after, {"meets_the_brief"}, t) == "plateau"


def test_classify_plateau_when_nothing_changes() -> None:
    t = Thresholds()
    before = {"meets_the_brief": 80.0}
    after = {"meets_the_brief": 80.0}
    assert classify(before, after, {"meets_the_brief"}, t) == "plateau"


def test_classify_improved_when_an_untargeted_dimension_also_rises() -> None:
    t = Thresholds()
    before = {"meets_the_brief": 80.0, "testing": 50.0}
    after = {"meets_the_brief": 82.0, "testing": 95.0}
    assert classify(before, after, {"meets_the_brief"}, t) == "improved"


def test_classify_plateau_when_only_untargeted_dimension_rises() -> None:
    t = Thresholds()
    before = {"meets_the_brief": 80.0, "testing": 50.0}
    after = {"meets_the_brief": 80.0, "testing": 95.0}
    assert classify(before, after, {"meets_the_brief"}, t) == "plateau"


def test_classify_regression_beats_improvement_elsewhere() -> None:
    t = Thresholds()
    before = {"meets_the_brief": 80.0, "testing": 90.0}
    after = {"meets_the_brief": 95.0, "testing": 80.0}
    assert classify(before, after, {"meets_the_brief"}, t) == "regression"


def test_classify_ignores_dimensions_missing_from_before() -> None:
    t = Thresholds()
    before: dict[str, float] = {}
    after = {"meets_the_brief": 50.0}
    assert classify(before, after, {"meets_the_brief"}, t) == "plateau"


def test_below_floor_returns_dimensions_under_the_floor() -> None:
    t = Thresholds()
    scores = {"meets_the_brief": 60.0, "testing": 90.0}
    assert below_floor(scores, t) == ["meets_the_brief"]


def test_below_floor_boundary_exactly_at_floor_is_not_included() -> None:
    t = Thresholds()
    scores = {"meets_the_brief": 70.0}
    assert below_floor(scores, t) == []


def test_below_floor_empty_when_nothing_is_below() -> None:
    t = Thresholds()
    scores = {"meets_the_brief": 90.0}
    assert below_floor(scores, t) == []
