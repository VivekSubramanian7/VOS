"""Operator CLI.

The interesting command is `reclassify`. Because the journal is the source of truth,
improving the prompt or switching model is retroactive: replay history and the whole
graph reflects the improvement, rather than only thoughts captured from now on.

`--dry-run` is the safety rail — it reports what *would* change without touching the
projection, so a prompt edit or a provider swap can be inspected before it lands.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import typer

from vos.cassette import Cassette
from vos.graph import Neo4jGraph
from vos.journal import JsonlJournal
from vos.pipeline import ThoughtState, build_pipeline, load_model
from vos.projection import classify_missing, rebuild, reproject_missing, select
from vos.settings import get_settings

app = typer.Typer(add_completion=False, help="VOS operator commands.")
log = logging.getLogger(__name__)


def _components(model_override: str | None = None, *, need_model: bool = True):
    """Wire the pieces a command needs.

    `need_model=False` for read-only commands. `stats` reports what is already in the
    journal and the graph and never calls a model, so a missing or misconfigured
    provider key must not stop it from answering — that is precisely the moment you
    are trying to find out what state things are in.
    """
    settings = get_settings()
    model_spec = model_override or settings.vos_model
    journal = JsonlJournal(settings.vos_journal_dir)
    cassette = Cassette(settings.vos_cassette_dir)
    graph = Neo4jGraph.connect(
        settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password.get_secret_value()
    )
    pipeline = (
        build_pipeline(load_model(model_spec), model_name=model_spec, cassette=cassette)
        if need_model
        else None
    )
    return settings, journal, graph, pipeline, model_spec


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


@app.command()
def reclassify(
    since: str = typer.Option(None, "--from", help="Only thoughts captured on/after YYYY-MM-DD"),
    model: str = typer.Option(None, "--model", help="Override VOS_MODEL for this run"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report changes without writing"),
    wipe: bool = typer.Option(
        False, "--rebuild", help="Wipe the projection first and replay from scratch"
    ),
) -> None:
    """Replay the journal through the classifier."""
    asyncio.run(_reclassify(since, model, dry_run, wipe))


async def _reclassify(since_raw, model_override, dry_run, wipe) -> None:
    settings, journal, graph, pipeline, model_spec = _components(model_override)
    since = _parse_date(since_raw)
    try:
        await graph.ensure_schema()
        records = select(journal.records(), since)
        if not records:
            typer.echo("Nothing in the journal to replay.")
            return

        typer.echo(f"{len(records)} thought(s) · model={model_spec}")

        if dry_run:
            # Never writes. This is what makes trying a new prompt or provider safe.
            before = await graph.categories_by_id()
            changes: list[tuple[str, str, str]] = []
            counts: Counter[str] = Counter()

            for i, record in enumerate(records, start=1):
                followed = await graph.following()
                out = await pipeline.ainvoke(
                    ThoughtState(record=record, followed=followed)
                )
                new = out.get("classification")
                if new is None:
                    counts["failed"] += 1
                    continue
                old = before.get(record.id, "—")
                counts["same" if old == new.category else "changed"] += 1
                if old != new.category:
                    changes.append((record.text[:60], old, new.category))
                typer.echo(f"\r  {i}/{len(records)}", nl=False)

            typer.echo("\n")
            for text, old, new in changes:
                typer.echo(f"  {old:>15} → {new:<15} {text}")
            typer.echo(
                f"\nunchanged={counts['same']} changed={counts['changed']} "
                f"failed={counts['failed']}"
            )
            typer.echo("Dry run — nothing was written.")
            return

        def progress(i: int, total: int, _record) -> None:
            typer.echo(f"\r  {i}/{total}", nl=False)

        ok, failed = await rebuild(
            journal, graph, pipeline, since=since, wipe=wipe, progress=progress
        )
        typer.echo(f"\nFiled {ok}." + (f" {failed} failed — see /pending." if failed else ""))
    finally:
        await graph.close()


@app.command()
def pending(
    limit: int = typer.Option(None, "--limit", help="Stop after N thoughts"),
) -> None:
    """Classify everything currently unfiled (failed, deferred, or recovered)."""
    asyncio.run(_pending(limit))


async def _pending(limit: int | None) -> None:
    _, journal, graph, pipeline, _ = _components()
    try:
        await graph.ensure_schema()
        recovered = await reproject_missing(journal, graph)
        if recovered:
            typer.echo(f"Recovered {len(recovered)} thought(s) missing from the graph.")
        ok, failed = await classify_missing(journal, graph, pipeline, limit)
        typer.echo(f"Filed {ok}." + (f" {failed} still failing." if failed else ""))
    finally:
        await graph.close()


@app.command()
def stats() -> None:
    """Counts, pending, top sources, and spend."""
    asyncio.run(_stats())


async def _stats() -> None:
    settings, journal, graph, _, _ = _components(need_model=False)
    cassette = Cassette(settings.vos_cassette_dir)
    try:
        s = await graph.stats()
        typer.echo(f"journal : {len(journal.records())} live records")
        typer.echo(f"graph   : {s.total} thoughts, {s.pending} pending")
        for name, n in sorted(s.by_category.items(), key=lambda kv: -kv[1]):
            typer.echo(f"          {name}: {n}")
        today = datetime.now(UTC).date()
        spent = sum(
            e.cost_usd or 0.0
            for e in cassette.all_entries()
            if e.created_at.astimezone(UTC).date() == today
        )
        typer.echo(f"spend   : ${spent:.4f} today (limit ${settings.vos_daily_budget_usd:.2f})")
    finally:
        await graph.close()


@app.command()
def doctor(
    live: bool = typer.Option(
        False, "--live", help="Also make one real model call (costs a fraction of a cent)"
    ),
) -> None:
    """Check that configuration and the database are usable."""
    asyncio.run(_doctor(live))


def _check_model(spec: str) -> bool:
    """Can the configured model be built, and is its key present?

    Worth a check because the alternative diagnosis path is bad: a missing or empty
    provider key surfaces as an exception *inside* the classifier, which §4.2 correctly
    degrades to `status=unclassified` in /pending. The symptom is then "the provider is
    down" rather than "there is no key", and the two have nothing in common.

    Providers disagree about when they notice. Gemini and Ollama raise while building;
    Anthropic builds happily and only fails on the call — hence `--live`.
    """
    try:
        load_model(spec)
    except ImportError as exc:
        typer.echo(f"✗ model {spec}: adapter not installed")
        typer.echo(f"  {exc}")
        return False
    except Exception as exc:  # noqa: BLE001 — providers raise their own types
        # A pydantic ValidationError sandwiches the useful sentence between a header
        # ("1 validation error for X") and boilerplate (input echo, docs URL). Neither
        # end is the message, so drop the known noise and take what remains.
        noise = ("For further information", "[type=", "1 validation error")
        lines = [
            ln.strip()
            for ln in str(exc).splitlines()
            if ln.strip() and not ln.strip().startswith(noise)
        ]
        detail = lines[0] if lines else type(exc).__name__
        # pydantic appends "[type=..., input_value=...]" to the same line; the input
        # echo can also contain config, so cutting it is hygiene as well as brevity.
        detail = detail.split(" [type=")[0].removeprefix("Value error, ")
        typer.echo(f"✗ model {spec}: {detail}")
        provider = spec.split(":", 1)[0]
        typer.echo(
            f"  Set the key {provider} expects in .env (GOOGLE_API_KEY for google_genai,\n"
            f"  ANTHROPIC_API_KEY for anthropic, ...). VOS never names it — the adapter\n"
            f"  reads it from the environment, so a new provider stays a config change."
        )
        return False

    typer.echo(f"✓ model {spec} resolves")
    return True


async def _xai_get(url: str, **kwargs) -> Any:
    import httpx

    async with httpx.AsyncClient(timeout=30.0) as client:
        return await client.get(url, **kwargs)


async def _check_xai_live(api_key: str, base_url: str, model: str) -> bool:
    """Prove the key works and the model name is real, without spending anything.

    Live Search bills per source, so `doctor` lists models rather than running a
    search. That also catches the failure most likely to bite — xAI renames models,
    and a wrong name would otherwise surface as a mid-digest error.
    """
    try:
        response = await _xai_get(
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        response.raise_for_status()
        names = {m.get("id") for m in response.json().get("data", [])}
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"✗ xAI: {type(exc).__name__}: {exc}")
        return False

    if model not in names:
        typer.echo(f"✗ xAI: model {model} not available")
        typer.echo(f"  Available: {', '.join(sorted(names)) or '(none returned)'}")
        typer.echo("  Set VOS_PULSE_MODEL in .env to one of these.")
        return False

    typer.echo(f"✓ xAI key works, model {model} available")
    return True


async def _live_call(spec: str) -> bool:
    """One real classification. The only check that proves the key actually works."""
    from vos.contracts import CaptureRecord, thought_id

    record = CaptureRecord(
        id=thought_id(0, 0),
        chat_id=0,
        message_id=0,
        text="Book flights to Kyoto for the October trip.",
        captured_at=datetime.now(UTC),
    )
    pipeline = build_pipeline(load_model(spec), model_name=spec)
    out = await pipeline.ainvoke(ThoughtState(record=record, followed=[]))
    if err := out.get("error"):
        typer.echo(f"✗ live call failed: {err}")
        return False
    c = out["classification"]
    typer.echo(f"✓ live call OK — classified as {c.category} (confidence {c.confidence:.2f})")
    return True


async def _doctor(live: bool = False) -> None:
    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"✗ settings: {exc}")
        typer.echo("  Copy .env.example to .env and fill it in.")
        raise typer.Exit(1) from exc

    typer.echo("✓ settings loaded")
    typer.echo(f"✓ journal dir {settings.vos_journal_dir}")

    model_ok = _check_model(settings.vos_model)

    graph = Neo4jGraph.connect(
        settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password.get_secret_value()
    )
    try:
        await graph.ensure_schema()
        s = await graph.stats()
        typer.echo(f"✓ neo4j at {settings.neo4j_uri} ({s.total} thoughts)")
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"✗ neo4j: {exc}")
        if "Unauthorized" in str(exc) or "authentication" in str(exc).lower():
            # Distinguished from "not running" because the remedy is completely
            # different — and the generic message sends you looking in the wrong place.
            typer.echo(
                "\n  Authentication failed, so Neo4j IS running — the password differs.\n"
                "  Neo4j writes the password into its data volume the first time it\n"
                "  starts; editing .env afterwards does not change it.\n\n"
                "  Recreate the volume (safe — the graph is a projection):\n"
                "      docker compose down -v && docker compose up -d neo4j\n"
                "      vos reclassify --rebuild     # replay the journal into it"
            )
        else:
            typer.echo("  Is it running?  docker compose up -d neo4j")
        raise typer.Exit(1) from exc
    finally:
        await graph.close()

    if live and model_ok:
        model_ok = await _live_call(settings.vos_model)
    elif live:
        typer.echo("· skipping --live: the model could not be built")

    if settings.xai_api_key is None:
        typer.echo("• xAI key not set — /pulse is disabled (everything else is fine)")
    elif live:
        await _check_xai_live(
            settings.xai_api_key.get_secret_value(),
            settings.vos_xai_base_url,
            settings.vos_pulse_model,
        )
    else:
        typer.echo("✓ xAI key present (use --live to verify it)")

    if not model_ok:
        # Capture would still work — the journal does not need a model. Say so, so the
        # failure reads as "enrichment is degraded", not "the system is down".
        typer.echo("\nCapture would still work; classification would land in /pending.")
        raise typer.Exit(1)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    app()


if __name__ == "__main__":
    main()
