from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

DIMENSIONS: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    (
        "meets_the_brief",
        "Meets the brief",
        (
            ("does_what_was_asked", "Does what the brief asked."),
            ("nothing_extra", "Does nothing that the brief did not ask for."),
        ),
    ),
    (
        "testing",
        "Testing",
        (
            ("happy_path", "Happy path."),
            ("edge_cases", "Known, possible edge cases."),
            ("proves_the_brief", "Tests prove what the brief said they must prove."),
            ("no_empty_or_skipped", "No empty or skipped tests."),
        ),
    ),
    (
        "error_handling",
        "Error handling",
        (
            ("api_and_io_errors", "API and I/O errors handled."),
            ("specific_and_catch_all", "Specific error types caught, plus a catch-all."),
            ("failures_reported", "Failures reported, not swallowed."),
        ),
    ),
    (
        "security",
        "Security",
        (
            ("input_validation", "Input validation."),
            ("injection", "Injection."),
            ("secrets", "Secrets."),
            ("authn_and_authz", "Authentication and authorization."),
            ("unsafe_defaults", "Unsafe defaults."),
            ("new_dependencies", "New dependencies."),
        ),
    ),
    (
        "architecture",
        "Architecture",
        (
            ("patterns_followed", "The project's architecture patterns are followed."),
            ("guidelines_followed", "Guidelines followed."),
            ("contracts_honored", "Contracts honored."),
            ("right_file", "Code lives in the right file."),
            ("departures_recorded", "Departures recorded."),
        ),
    ),
    (
        "code_structure",
        "Code structure",
        (
            ("single_responsibility", "Each function does one thing."),
            ("modular", "Modular."),
            ("reusable", "Reusable."),
            ("no_duplication", "No duplicated code."),
        ),
    ),
    (
        "performance",
        "Performance",
        (
            ("no_needless_work", "No needless work."),
            ("complexity_fits_data_size", "Complexity fits the data size."),
            ("sensible_resource_use", "Sensible use of memory, I/O, and network."),
        ),
    ),
    (
        "maintainability",
        "Maintainability",
        (
            ("clear_names", "Clear names."),
            ("small_units", "Small units."),
            ("minimal_comments", "Minimal comments: only where the project's rules call for one."),
        ),
    ),
    (
        "accessibility",
        "Accessibility",
        (
            ("ui_files_only", "UI files only."),
            (
                "accessibility_tools_check",
                "Checked with the accessibility-tools plugin when it is installed.",
            ),
        ),
    ),
)

DIMENSION_KEYS: tuple[str, ...] = tuple(dimension[0] for dimension in DIMENSIONS)

_CRITERIA_BY_DIMENSION: dict[str, tuple[str, ...]] = {
    key: tuple(criterion_key for criterion_key, _ in criteria) for key, _, criteria in DIMENSIONS
}

RATING_MIN = 1
RATING_MAX = 10
RATING_RANGE = f"{RATING_MIN}..{RATING_MAX}"
REASON_REQUIRED_BELOW = 9
# Fixed by the PRD, apart from Thresholds.criterion_floor, which the settings can change.
ISSUE_AT_OR_BELOW = 4
ISSUE_RESOLVED_AT = ISSUE_AT_OR_BELOW + 1

RATING_SHAPE = (
    f'each rating is {{"dimension": <key>, "criterion": <key>, "value": {RATING_RANGE}, '
    f'"reason": <text, required below {REASON_REQUIRED_BELOW}>, '
    f'"ref": <file:line, required below {REASON_REQUIRED_BELOW}>}}; '
    "applicable maps every dimension key to null or a one-line reason it does not apply"
)

REVIEW_DIMENSIONS: tuple[str, ...] = ("completeness", "integration", "open_items")

REVIEW_SCORE_SHAPE = (
    f'each score is {{"dimension": <key>, "value": {RATING_RANGE}, '
    f'"reason": <text, required below {REASON_REQUIRED_BELOW}>}}; '
    f"required dimensions: {list(REVIEW_DIMENSIONS)}"
)


def schema_help() -> str:
    parts = [f"{key}: {', '.join(criteria)}" for key, criteria in _CRITERIA_BY_DIMENSION.items()]
    return RATING_SHAPE + ". Keys: " + "; ".join(parts)


def ratings_schema() -> dict[str, Any]:
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "dimension": {"type": "string", "enum": list(DIMENSION_KEYS)},
                "criterion": {"type": "string"},
                "value": {"type": "integer", "minimum": RATING_MIN, "maximum": RATING_MAX},
                "reason": {
                    "type": "string",
                    "description": f"Required below {REASON_REQUIRED_BELOW}.",
                },
                "ref": {
                    "type": "string",
                    "description": f"file:line; required below {REASON_REQUIRED_BELOW}.",
                },
            },
            "required": ["dimension", "criterion", "value"],
            "oneOf": [
                {
                    "properties": {
                        "dimension": {"const": key},
                        "criterion": {"enum": list(criteria)},
                    }
                }
                for key, criteria in _CRITERIA_BY_DIMENSION.items()
            ],
        },
    }


def applicable_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {key: {"type": ["string", "null"]} for key in DIMENSION_KEYS},
        "required": list(DIMENSION_KEYS),
        "additionalProperties": False,
    }


def dimensions_schema() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string", "enum": list(DIMENSION_KEYS)}}


@dataclass
class Thresholds:
    target: int = 90
    floor: int = 70
    criterion_floor: int = 5
    disagreement_gap: int = 10
    plateau: int = 2
    regression_tolerance: int = 5


@dataclass
class Rating:
    dimension: str
    criterion: str
    value: int
    reason: str | None
    ref: str | None


@dataclass
class ReviewScore:
    dimension: str
    value: int
    reason: str | None


def _checked_entry(raw: object, label: str, keys: tuple[str, ...], shape: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"a {label} must be an object, got {type(raw).__name__}. {shape}")
    missing = [key for key in keys if key not in raw]
    if missing:
        raise ValueError(f"{label} {sorted(raw.keys())} is missing {missing}. {shape}")
    value = raw["value"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} value must be an integer {RATING_RANGE}, got {value!r}")
    return raw


def parse_rating(raw: object) -> Rating:
    entry = _checked_entry(raw, "rating", ("dimension", "criterion", "value"), schema_help())
    return Rating(
        str(entry["dimension"]),
        str(entry["criterion"]),
        entry["value"],
        entry.get("reason"),
        entry.get("ref"),
    )


def parse_review_score(raw: object) -> ReviewScore:
    entry = _checked_entry(raw, "score", ("dimension", "value"), REVIEW_SCORE_SHAPE)
    return ReviewScore(str(entry["dimension"]), entry["value"], entry.get("reason"))


def validate_review_scores(scores: list[ReviewScore]) -> None:
    seen = {s.dimension for s in scores}
    missing = [d for d in REVIEW_DIMENSIONS if d not in seen]
    if missing:
        raise ValueError(f"scores is missing {missing}. {REVIEW_SCORE_SHAPE}")
    unknown = sorted(seen - set(REVIEW_DIMENSIONS))
    if unknown:
        raise ValueError(f"unknown score dimension(s) {unknown}. {REVIEW_SCORE_SHAPE}")
    for score in scores:
        if not RATING_MIN <= score.value <= RATING_MAX:
            raise ValueError(f"{score.dimension}: value {score.value} is outside {RATING_RANGE}")
        if score.value < REASON_REQUIRED_BELOW and not score.reason:
            raise ValueError(
                f"{score.dimension}: a rating below {REASON_REQUIRED_BELOW} needs a reason"
            )


def validate_ratings(ratings: list[Rating], applicable: dict[str, str | None]) -> None:
    rated: dict[str, set[str]] = {}
    for rating in ratings:
        if rating.dimension not in _CRITERIA_BY_DIMENSION:
            raise ValueError(f"unknown dimension {rating.dimension!r}. {schema_help()}")
        if rating.criterion not in _CRITERIA_BY_DIMENSION[rating.dimension]:
            raise ValueError(
                f"unknown criterion {rating.criterion!r} for dimension {rating.dimension!r}; "
                f"its criteria are {list(_CRITERIA_BY_DIMENSION[rating.dimension])}"
            )
        label = f"{rating.dimension}.{rating.criterion}"
        if not RATING_MIN <= rating.value <= RATING_MAX:
            raise ValueError(f"{label}: value {rating.value} is outside {RATING_RANGE}")
        if rating.value < REASON_REQUIRED_BELOW and not rating.reason:
            raise ValueError(f"{label}: a rating below {REASON_REQUIRED_BELOW} needs a reason")
        if rating.value < REASON_REQUIRED_BELOW and not rating.ref:
            raise ValueError(
                f"{label}: a rating below {REASON_REQUIRED_BELOW} needs a ref (file:line)"
            )
        rated.setdefault(rating.dimension, set()).add(rating.criterion)

    unlisted = [key for key in DIMENSION_KEYS if key not in applicable]
    if unlisted:
        raise ValueError(
            f"every dimension is scored on every review; applicable is missing {unlisted}: "
            "rate each one, or mark it not applicable with a one-line reason. "
            f"{schema_help()}"
        )
    for dimension, reason in applicable.items():
        if dimension not in _CRITERIA_BY_DIMENSION:
            raise ValueError(f"unknown dimension {dimension!r} in applicable. {schema_help()}")
        if reason is not None and not str(reason).strip():
            raise ValueError(f"{dimension}: a dimension marked not applicable needs a reason")
        if reason is None:
            rated_for_dimension = rated.get(dimension, set())
            missing = [
                key for key in _CRITERIA_BY_DIMENSION[dimension] if key not in rated_for_dimension
            ]
            if missing:
                raise ValueError(f"{dimension}: missing ratings for criteria {missing}")
        elif dimension in rated:
            raise ValueError(f"{dimension}: rated but marked not applicable ({reason})")


def dimension_scores(ratings: list[Rating]) -> dict[str, float]:
    grouped: dict[str, list[int]] = {}
    for rating in ratings:
        grouped.setdefault(rating.dimension, []).append(rating.value)
    return {
        dimension: round((sum(values) / len(values)) * 10, 1)
        for dimension, values in grouped.items()
    }


def passes(
    scores: dict[str, float], ratings: list[Rating], t: Thresholds
) -> tuple[bool, list[str]]:
    reasons = [
        f"{dimension}: {score} is below the target of {t.target}"
        for dimension, score in scores.items()
        if score < t.target
    ]
    reasons.extend(
        f"{rating.dimension}.{rating.criterion}: {rating.value} is below the "
        f"criterion floor of {t.criterion_floor}"
        for rating in ratings
        if rating.value < t.criterion_floor
    )
    return (len(reasons) == 0, reasons)


def issues_from(ratings: list[Rating]) -> list[Rating]:
    return [rating for rating in ratings if rating.value <= ISSUE_AT_OR_BELOW]


def disagreements(a: dict[str, float], b: dict[str, float], t: Thresholds) -> list[str]:
    result: list[str] = []
    for dimension in DIMENSION_KEYS:
        if dimension not in a or dimension not in b:
            continue
        score_a, score_b = a[dimension], b[dimension]
        if abs(score_a - score_b) >= t.disagreement_gap:
            result.append(dimension)
        elif (score_a >= t.target) != (score_b >= t.target):
            result.append(dimension)
    return result


def classify(
    before: dict[str, float],
    after: dict[str, float],
    targeted: set[str],
    t: Thresholds,
) -> Literal["improved", "plateau", "regression"]:
    for dimension, after_score in after.items():
        before_score = before.get(dimension)
        if before_score is None:
            continue
        fell = before_score - after_score
        if fell >= t.regression_tolerance:
            return "regression"
        if fell > 0 and after_score < t.floor:
            return "regression"

    for dimension in targeted:
        before_score = before.get(dimension)
        after_score = after.get(dimension)
        if before_score is None or after_score is None:
            continue
        if after_score - before_score >= t.plateau:
            return "improved"

    return "plateau"
