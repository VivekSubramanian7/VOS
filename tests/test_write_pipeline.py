"""The compose node.

The assertions that matter are about what reaches the prompt and what comes back, not
about prose quality. Two failures would make this feature worse than useless: silently
dropping the author's own material (so every post reads like everyone else's), and
presenting an invented anecdote as theirs.
"""

from __future__ import annotations

from vos.contracts import LinkedInDraft
from vos.pipeline import WRITE_PROMPT, WriteState, _write_input, build_write_pipeline


class StubModel:
    """Records the messages it was given and returns one queued draft."""

    def __init__(self, result=None):
        self.result = result
        self.messages = None

    def with_structured_output(self, schema):  # noqa: ANN001
        outer = self

        class _R:
            async def ainvoke(self, messages):  # noqa: ANN001
                outer.messages = messages
                if isinstance(outer.result, Exception):
                    raise outer.result
                return outer.result

        return _R()


def _draft() -> LinkedInDraft:
    return LinkedInDraft(
        hook="Evals are mostly theatre.",
        body="We shipped weekly anyway.\n\nHere is what changed.",
        hashtags=["ai", "evals"],
        grounded_in="evals are mostly theatre",
    )


def _state(**kw) -> WriteState:
    base = dict(
        keywords=["evals"],
        trend_summary="Everyone is arguing about evals.",
        trend_posts=["@someone: evals are broken"],
        own_notes=["evals are mostly theatre"],
    )
    base.update(kw)
    return WriteState(**base)


# -- what reaches the model -------------------------------------------------- #


def test_the_authors_own_notes_reach_the_prompt():
    """If this ever silently stops, every draft still reads fine and is quietly generic,
    which is the failure mode hardest to notice."""
    text = _write_input(_state())
    assert "evals are mostly theatre" in text
    assert "THEIR OWN NOTES" in text


def test_the_trend_reaches_the_prompt_with_attribution():
    text = _write_input(_state())
    assert "@someone: evals are broken" in text
    assert "Everyone is arguing about evals." in text


def test_empty_notes_are_stated_loudly_rather_than_omitted():
    """An absent section reads to the model as 'nothing to mention'. An explicit one
    reads as 'do not invent'."""
    text = _write_input(_state(own_notes=[]))
    assert "none on this topic" in text
    assert "invent nothing" in text


def test_the_prompt_forbids_fabrication_in_terms():
    """The one instruction this feature cannot ship without."""
    assert "NEVER invent" in WRITE_PROMPT
    assert "grounded_in" in WRITE_PROMPT


def test_the_prompt_carries_the_measured_constraints():
    assert "140" in WRITE_PROMPT          # mobile see-more cutoff
    assert "1,300-2,100" in WRITE_PROMPT  # engagement sweet spot
    for banned in ("delve", "crucial", "robust", "tapestry"):
        assert banned in WRITE_PROMPT


# -- the node ---------------------------------------------------------------- #


async def test_compose_returns_a_draft():
    model = StubModel(_draft())
    graph = build_write_pipeline(model, model_name="stub")
    out = await graph.ainvoke(_state())
    assert out["error"] is None
    assert out["draft"].hook == "Evals are mostly theatre."


async def test_a_provider_failure_is_returned_not_raised():
    """The X search that fed this has already been paid for; losing the draft must not
    also lose the digest."""
    graph = build_write_pipeline(StubModel(RuntimeError("503")), model_name="stub")
    out = await graph.ainvoke(_state())
    assert out["draft"] is None
    assert out["error"] == "RuntimeError"


async def test_a_model_returning_nothing_usable_is_an_error_not_a_crash():
    graph = build_write_pipeline(StubModel(None), model_name="stub")
    out = await graph.ainvoke(_state())
    assert out["draft"] is None
    assert "usable" in out["error"]


async def test_the_node_reports_its_latency_for_the_cassette():
    """Recording happens in run_write, which needs the number from here."""
    graph = build_write_pipeline(StubModel(_draft()), model_name="stub")
    out = await graph.ainvoke(_state())
    assert out["latency_ms"] >= 0
