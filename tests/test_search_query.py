"""Building the Lucene query — the half of the search fix that needs no database.

`db.index.fulltext.queryNodes` takes a query *language*, and the original code handed
it the user's raw words. Three separate failures followed, and each one has a test
here: words were OR-ed together, `*` meant everything, and `(` was a syntax error that
reached the user as no reply at all.
"""

from __future__ import annotations

import pytest

from vos.graph import _lucene_query


def test_every_word_is_required():
    """The reported bug: `/search trip to rome` returned every thought with "to"."""
    assert _lucene_query("trip to rome") == "+trip +to +rome"


def test_any_mode_drops_the_requirement():
    """The deliberate fallback, used only after strict has found nothing."""
    assert _lucene_query("trip to rome", match="any") == "trip to rome"


def test_a_single_word_is_still_required():
    assert _lucene_query("bananas") == "+bananas"


def test_whitespace_is_collapsed():
    assert _lucene_query("  oat   milk \n") == "+oat +milk"


@pytest.mark.parametrize(
    ("term", "expected"),
    [
        ("*", r"+\*"),
        ("?", r"+\?"),
        ("(", r"+\("),
        ("C++", r"+C\+\+"),
        ("foo:bar", r"+foo\:bar"),
        ("rome~", r"+rome\~"),
        ('"quoted"', r"+\"quoted\""),
        ("a&&b", r"+a\&\&b"),
        ("back\\slash", r"+back\\slash"),
    ],
)
def test_lucene_syntax_is_escaped(term: str, expected: str):
    """Every operator becomes a literal character.

    `*` used to match the entire graph and `(` used to raise a ClientError out of the
    handler. Escaping is what makes both impossible rather than merely unlikely.
    """
    assert _lucene_query(term) == expected


def test_escaping_applies_in_any_mode_too():
    assert _lucene_query("*", match="any") == r"\*"


def test_nothing_searchable_returns_none():
    """Signals "don't bother the database", distinct from "found nothing"."""
    assert _lucene_query("") is None
    assert _lucene_query("   \t\n ") is None


def test_stop_words_are_left_alone():
    """Deliberately *not* filtered here.

    The `english` analyzer on the index strips them from the query as well as from the
    text, so a hand-written stop list in this repo would be a second, drifting copy of
    a vocabulary Lucene already ships.
    """
    assert _lucene_query("the bananas") == "+the +bananas"
