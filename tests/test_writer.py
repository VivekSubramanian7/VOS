"""The LinkedIn writer, offline.

No network and no model. The two properties under test are the ones that decide whether
this feature is worth having: the draft must be grounded in the author's own material,
and it must never claim to be when it isn't.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from vos.contracts import LinkedInDraft, WriteError, draft_id
from vos.writer import (
    MAX_KEYWORDS,
    MAX_OWN_NOTES,
    DraftStore,
    build_artifact,
    gather_own_material,
    parse_keywords,
    search_term,
    trend_lines,
)

NOW = datetime(2026, 8, 19, 9, 0, tzinfo=UTC)


class _View:
    """Shaped like a ThoughtView: `id` is a UUID, as the graph returns."""

    def __init__(self, text: str, id_=None) -> None:
        self.text = text
        self.id = id_ or uuid4()


class FakeGraph:
    """Records what was searched and replays canned results."""

    def __init__(self, thoughts=None, notes=None, fail: bool = False) -> None:
        self._thoughts = thoughts or []
        self._notes = notes or []
        self._fail = fail
        self.terms: list[tuple[str, str]] = []

    async def search(self, term, n=10, *, match="all"):
        if self._fail:
            raise RuntimeError("fulltext index missing")
        self.terms.append((term, match))
        return self._thoughts[:n]

    async def search_notes(self, term, n=10):
        if self._fail:
            raise RuntimeError("fulltext index missing")
        return self._notes[:n]


# -- keywords ---------------------------------------------------------------- #


def test_comma_separated_keywords_are_split_and_trimmed():
    assert parse_keywords("ai agents, evals , rag") == ["ai agents", "evals", "rag"]


def test_a_single_keyword_is_fine():
    assert parse_keywords("evals") == ["evals"]


def test_empty_input_refuses_rather_than_writing_about_nothing():
    """Silently proceeding would spend the price of an X search on whatever the model
    felt like writing about."""
    with pytest.raises(WriteError, match="keywords"):
        parse_keywords("   ,  , ")


def test_a_pasted_wall_of_keywords_is_capped():
    out = parse_keywords(",".join(f"k{i}" for i in range(40)))
    assert len(out) == MAX_KEYWORDS


def test_search_term_joins_for_the_fulltext_index():
    assert search_term(["ai agents", "evals"]) == "ai agents evals"


# -- own material ------------------------------------------------------------ #


async def test_the_authors_own_thoughts_are_gathered():
    graph = FakeGraph(thoughts=[_View("evals are mostly theatre"), _View("we ship weekly")])
    lines, ids = await gather_own_material(graph, ["evals"])
    assert lines == ["evals are mostly theatre", "we ship weekly"]
    assert len(ids) == 2


async def test_the_search_matches_any_keyword_not_all_of_them():
    """`all` is the default elsewhere and would be wrong here: three loosely related
    keywords rarely co-occur in one thought, so every draft would come back ungrounded."""
    graph = FakeGraph(thoughts=[_View("something")])
    await gather_own_material(graph, ["ai agents", "evals", "rag"])
    assert graph.terms == [("ai agents evals rag", "any")]


async def test_video_notes_top_up_a_thin_thought_search():
    graph = FakeGraph(thoughts=[_View("one thought")], notes=[_View("a distilled claim")])
    lines, _ = await gather_own_material(graph, ["evals"])
    assert lines == ["one thought", "a distilled claim"]


async def test_material_is_capped():
    graph = FakeGraph(thoughts=[_View(f"thought {i}") for i in range(50)])
    lines, _ = await gather_own_material(graph, ["evals"])
    assert len(lines) == MAX_OWN_NOTES


async def test_a_broken_graph_yields_no_material_rather_than_an_error():
    """An empty graph, a missing index and a genuinely new topic look identical from
    here, and none of them is a reason to lose the draft."""
    lines, ids = await gather_own_material(FakeGraph(fail=True), ["evals"])
    assert lines == []
    assert ids == []


# -- trend material ---------------------------------------------------------- #


class _Post:
    def __init__(self, text, handle="@someone", url="https://x.com/a/status/1"):
        self.text = text
        self.author_handle = handle
        self.url = url


class _Digest:
    def __init__(self, summary="", posts=None):
        self.summary = summary
        self.posts = posts or []


def test_trend_lines_carry_the_handle_so_the_model_can_attribute():
    summary, posts = trend_lines(_Digest("A quiet week.", [_Post("evals are broken")]))
    assert summary == "A quiet week."
    assert posts == ["@someone: evals are broken"]


def test_a_missing_digest_is_not_an_error():
    """A failed X search still leaves the author's own notes, which is the substance."""
    assert trend_lines(None) == ("", [])


# -- the draft itself -------------------------------------------------------- #


def _draft(**kw) -> LinkedInDraft:
    base = dict(hook="Evals are theatre.", body="First point.\n\nSecond point.", hashtags=["ai"])
    base.update(kw)
    return LinkedInDraft(**base)


def test_the_post_text_assembles_hook_body_and_tags():
    text = _draft().text
    assert text.startswith("Evals are theatre.")
    assert "#ai" in text
    assert "\n\n" in text


def test_a_hash_prefix_the_model_added_anyway_is_not_doubled():
    assert "##" not in _draft(hashtags=["#ai", "evals"]).text


def test_a_hook_longer_than_the_mobile_cutoff_is_rejected():
    """210 is the ceiling because LinkedIn truncates near 140 on mobile; a hook that
    needs the tap to make sense has already lost the reader."""
    with pytest.raises(ValueError):
        _draft(hook="x" * 250)


def test_grounded_in_is_optional_and_defaults_to_none():
    """None is the honest answer when the author's notes held nothing, and the renderer
    says so. The alternative -- inventing an anecdote -- publishes a lie under their name."""
    assert _draft().grounded_in is None


def test_the_draft_is_cached_to_disk(tmp_path: Path):
    artifact = build_artifact(
        _draft(),
        keywords=["evals"],
        model="stub",
        source_posts=["https://x.com/a/status/1"],
        source_thoughts=[],
        now=NOW,
    )
    DraftStore(tmp_path).write(artifact)
    written = list((tmp_path / "drafts").glob("*.json"))
    assert len(written) == 1
    assert written[0].stem == str(draft_id(["evals"], NOW))


# -- run_write --------------------------------------------------------------- #


class _Pipeline:
    def __init__(self, out):
        self.out = out
        self.state = None

    async def ainvoke(self, state):
        self.state = state
        return self.out


async def test_run_write_records_the_spend_and_caches_the_draft(tmp_path: Path):
    """The X search is the expensive half and is already paid for by this point, so the
    cost travels with the draft rather than being re-derived."""
    from vos.projection import run_write

    recorded = []

    class _C:
        def record(self, entry):
            recorded.append(entry)

    pipeline = _Pipeline({"draft": _draft(), "error": None, "latency_ms": 12})
    graph = FakeGraph(thoughts=[_View("evals are mostly theatre")])

    result = await run_write(
        pipeline, graph, ["evals"], _Digest("trend", [_Post("x")]),
        artifact_dir=tmp_path, model_name="stub", cost_usd=0.22, cassette=_C(),
    )

    assert result.ok
    assert result.cost_usd == 0.22
    assert result.source_thoughts == 1
    assert result.source_posts == 1
    assert recorded and "(write)" in recorded[0].model
    assert list((tmp_path / "drafts").glob("*.json"))


async def test_run_write_reports_a_compose_failure_without_raising(tmp_path: Path):
    from vos.projection import run_write

    pipeline = _Pipeline({"draft": None, "error": "RuntimeError", "latency_ms": 0})
    result = await run_write(
        pipeline, FakeGraph(), ["evals"], None, artifact_dir=tmp_path, model_name="stub"
    )
    assert not result.ok
    assert result.error == "RuntimeError"


async def test_run_write_still_writes_when_the_x_search_gave_nothing(tmp_path: Path):
    """A failed digest leaves the author's own notes, which is where the substance is."""
    from vos.projection import run_write

    pipeline = _Pipeline({"draft": _draft(), "error": None, "latency_ms": 5})
    graph = FakeGraph(thoughts=[_View("evals are mostly theatre")])
    result = await run_write(
        pipeline, graph, ["evals"], None, artifact_dir=tmp_path, model_name="stub"
    )
    assert result.ok
    assert result.source_posts == 0
    assert result.source_thoughts == 1
