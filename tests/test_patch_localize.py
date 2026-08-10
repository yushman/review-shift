"""Localization core (ADR-013 §1-2) — the highest-risk logic in the product."""
from review_shift.patch import localize


def test_single_match_in_window_is_applicable():
    file_text = "a\nb\nc\nd\ne\n"
    status, start, end = localize("c", file_text, line=3)
    assert status == "applicable"
    assert (start, end) == (2, 2)


def test_multiline_before_matches_correctly():
    file_text = "a\nb\nc\nd\ne\n"
    status, start, end = localize("b\nc\nd", file_text, line=2)
    assert status == "applicable"
    assert (start, end) == (1, 3)


def test_no_match_anywhere_is_stale():
    file_text = "a\nb\nc\n"
    status, start, end = localize("zzz", file_text, line=1)
    assert status == "stale"
    assert start is None and end is None


def test_two_equidistant_matches_are_ambiguous():
    # "x" at line 2 and line 4, reported line 3 -> both distance 1
    file_text = "a\nx\nc\nx\ne\n"
    status, start, end = localize("x", file_text, line=3)
    assert status == "ambiguous"


def test_nearest_of_multiple_matches_in_window_wins():
    # "x" at line 2 and line 10, reported line 3 -> nearest is line 2
    file_text = "\n".join(["x" if i in (2, 10) else str(i) for i in range(1, 15)])
    status, start, end = localize("x", file_text, line=3)
    assert status == "applicable"
    assert start == 1  # 0-based index of line 2


def test_match_only_outside_window_falls_back_to_whole_file():
    # before at line 50, reported line 1 -> outside default window of 20
    lines = [f"n{i}" for i in range(1, 60)]
    lines[49] = "TARGET"
    file_text = "\n".join(lines)
    status, start, end = localize("TARGET", file_text, line=1)
    assert status == "applicable"
    assert start == 49


def test_multiple_matches_outside_window_is_ambiguous():
    lines = [f"n{i}" for i in range(1, 60)]
    lines[49] = "TARGET"
    lines[55] = "TARGET"
    file_text = "\n".join(lines)
    status, start, end = localize("TARGET", file_text, line=1)
    assert status == "ambiguous"


def test_trailing_whitespace_and_crlf_are_normalized_for_matching():
    file_text = "a\r\nb   \r\nc\r\n"
    status, start, end = localize("b", file_text, line=2)
    assert status == "applicable"
    assert (start, end) == (1, 1)


def test_bom_is_stripped_for_matching():
    file_text = "﻿a\nb\nc\n"
    status, start, end = localize("a", file_text, line=1)
    assert status == "applicable"
    assert (start, end) == (0, 0)


def test_indentation_is_not_normalized():
    file_text = "def f():\n    return 1\n"
    status, _, _ = localize("    return 1", file_text, line=2)
    assert status == "applicable"
    # wrong indentation must not match even though the text is otherwise identical
    status2, _, _ = localize("return 1", file_text, line=2)
    assert status2 == "stale"
    status3, _, _ = localize("\treturn 1", file_text, line=2)
    assert status3 == "stale"
