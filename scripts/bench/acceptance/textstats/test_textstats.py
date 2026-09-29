from __future__ import annotations

import json
from pathlib import Path

import pytest

TEXT = "The cat sat. The cat ran! Did the dog run? Don't stop."


def test_words_keep_inner_apostrophes() -> None:
    from textstats.core.tokenize import words

    assert words("Don't stop, don't!") == ["don't", "stop", "don't"]


def test_sentences_split_on_terminators() -> None:
    from textstats.core.tokenize import sentences

    assert len(sentences(TEXT)) == 4


def test_top_breaks_ties_alphabetically() -> None:
    from textstats.core.counts import frequencies, top

    assert frequencies(["b", "a", "b"]) == {"a": 1, "b": 2}
    assert top(["b", "a", "b", "c", "a"], 2) == [("a", 2), ("b", 2)]
    with pytest.raises(ValueError):
        top(["a"], 0)


@pytest.mark.parametrize(("word", "count"), [("cake", 1), ("banana", 3), ("the", 1), ("a", 1)])
def test_syllables(word: str, count: int) -> None:
    from textstats.core.readability import syllables

    assert syllables(word) == count


def test_flesch_rejects_empty_text() -> None:
    from textstats.core.readability import flesch_reading_ease

    with pytest.raises(ValueError):
        flesch_reading_ease("")


def test_version() -> None:
    import textstats

    assert textstats.__version__ == "0.1.0"


def test_json_report(run_python, tmp_path: Path) -> None:
    source = tmp_path / "in.txt"
    source.write_text(TEXT, encoding="utf-8")
    done = run_python("-m", "textstats", str(source), "--json", "--top", "2")
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)
    assert {"words", "sentences", "unique_words", "top", "flesch_reading_ease"} <= set(report)
    assert report["words"] == 12
    assert report["sentences"] == 4
    assert report["top"] == [["the", 3], ["cat", 2]]


def test_missing_file(run_python, tmp_path: Path) -> None:
    done = run_python("-m", "textstats", str(tmp_path / "nope.txt"))
    assert done.returncode == 2
    assert done.stderr.startswith("error: ")


def test_empty_text(run_python, tmp_path: Path) -> None:
    source = tmp_path / "empty.txt"
    source.write_text("", encoding="utf-8")
    done = run_python("-m", "textstats", str(source))
    assert done.returncode == 2
    assert done.stderr.strip() == "error: no text"
