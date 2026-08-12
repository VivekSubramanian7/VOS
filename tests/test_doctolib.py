"""Doctolib slot fetching, offline.

No network here. The transport is injectable precisely so this suite can exercise the
whole parse-and-cache path against payloads shaped like the real endpoint's, captured
from a live request rather than imagined.

The property under test throughout is that a wrong configuration says so. An empty slot
list is indistinguishable from a full calendar, so every way the request can be wrong has
to raise instead of returning nothing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from vos.contracts import DoctolibError
from vos.doctolib import (
    MAX_DAYS,
    SlotFetcher,
    parse_availabilities_url,
    slots_from_payload,
)

NOW = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)

URL = (
    "https://www.doctolib.de/availabilities.json"
    "?visit_motive_ids=12439959&agenda_ids=1931917-2355212-2355213"
    "&practice_ids=601147&telehealth=false&start_date=2026-08-19&limit=5"
)


def _payload(*days: tuple[str, list[str]], total: int | None = None) -> dict:
    """A response in the real shape: one entry per day, most of them empty."""
    return {
        "availabilities": [
            {"date": d, "slots": s, "substitution": None, "appointment_request_slots": []}
            for d, s in days
        ],
        "total": total if total is not None else sum(len(s) for _, s in days),
    }


class StubTransport:
    """Records requested URLs and replays canned responses in order."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls: list[str] = []

    async def __call__(self, url: str) -> dict:
        self.urls.append(url)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _fetcher(tmp_path: Path, transport) -> SlotFetcher:
    return SlotFetcher(
        source_url=URL, artifact_dir=tmp_path, transport=transport, now=lambda: NOW
    )


# -- URL parsing ------------------------------------------------------------ #


def test_the_captured_url_yields_the_ids_the_endpoint_needs():
    params = parse_availabilities_url(URL)
    assert params["agenda_ids"] == "1931917-2355212-2355213"
    assert params["visit_motive_ids"] == "12439959"
    assert params["practice_ids"] == "601147"


def test_a_booking_page_url_is_rejected_with_a_usable_message():
    """The easy mistake: pasting the page you were looking at, not the XHR behind it."""
    with pytest.raises(DoctolibError, match="Network tab"):
        parse_availabilities_url(
            "https://www.doctolib.de/gemeinschaftspraxis/kornwestheim/x/booking/availabilities"
            "?specialityId=1287"
        )


def test_a_url_without_agenda_ids_is_rejected():
    """This is the whole reason the URL is copied rather than constructed: without
    agenda_ids the endpoint answers 200 with an empty list, which looks like a full
    calendar instead of a broken request."""
    with pytest.raises(DoctolibError, match="agenda_ids"):
        parse_availabilities_url(
            "https://www.doctolib.de/availabilities.json?visit_motive_ids=1&practice_ids=2"
        )


def test_a_non_doctolib_url_is_rejected():
    with pytest.raises(DoctolibError, match="Doctolib URL"):
        parse_availabilities_url("https://example.com/availabilities.json?agenda_ids=1")


def test_rejections_are_permanent():
    """Retrying a typo forever would be noise, not resilience."""
    with pytest.raises(DoctolibError) as exc:
        parse_availabilities_url("https://example.com/x")
    assert exc.value.permanent is True


# -- payload parsing -------------------------------------------------------- #


def test_slots_are_flattened_and_sorted():
    payload = _payload(
        ("2026-08-20", ["2026-08-20T14:00:00.000+02:00"]),
        ("2026-08-19", ["2026-08-19T10:20:00.000+02:00"]),
    )
    slots = slots_from_payload(payload)
    assert [s.starts_at.isoformat() for s in slots] == [
        "2026-08-19T10:20:00+02:00",
        "2026-08-20T14:00:00+02:00",
    ]


def test_the_practice_offset_is_preserved():
    """10:20 must stay the time on the practice's door, not become 08:20 UTC."""
    slots = slots_from_payload(_payload(("2026-08-19", ["2026-08-19T10:20:00.000+02:00"])))
    assert slots[0].starts_at.strftime("%H:%M") == "10:20"


def test_empty_days_are_not_slots():
    """Doctolib returns every day in the window, most with nothing free."""
    payload = _payload(("2026-08-20", []), ("2026-08-21", []))
    assert slots_from_payload(payload) == []


def test_one_unparseable_slot_does_not_cost_the_others():
    payload = _payload(("2026-08-19", ["not a timestamp", "2026-08-19T10:20:00.000+02:00"]))
    assert len(slots_from_payload(payload)) == 1


def test_a_response_with_no_availabilities_key_is_empty_not_an_error():
    assert slots_from_payload({}) == []


# -- SlotFetcher ------------------------------------------------------------ #


async def test_fetch_returns_the_slots(tmp_path: Path):
    transport = StubTransport(_payload(("2026-08-19", ["2026-08-19T10:20:00.000+02:00"])))
    snapshot = await _fetcher(tmp_path, transport).fetch()
    assert snapshot.total == 1
    assert snapshot.slots[0].starts_at.strftime("%H:%M") == "10:20"


async def test_the_window_is_ours_not_the_captured_urls(tmp_path: Path):
    """The pasted URL carries whatever start_date the browser happened to ask for.
    Reusing it would pin the answer to the day the user copied it."""
    transport = StubTransport(_payload())
    await _fetcher(tmp_path, transport).fetch()
    q = parse_qs(urlparse(transport.urls[0]).query)
    assert q["start_date"] == ["2026-08-12"]
    assert q["limit"] == [str(MAX_DAYS)]


def test_the_agenda_ids_survive_into_the_request(tmp_path: Path):
    url = _fetcher(tmp_path, StubTransport()).request_url()
    assert parse_qs(urlparse(url).query)["agenda_ids"] == ["1931917-2355212-2355213"]


def test_the_day_window_is_capped_at_the_endpoints_limit(tmp_path: Path):
    """Asking for more returns {"error":["limit: must be less than or equal to 15"]}."""
    url = _fetcher(tmp_path, StubTransport()).request_url(days=90)
    assert parse_qs(urlparse(url).query)["limit"] == [str(MAX_DAYS)]


async def test_the_snapshot_is_cached_to_disk(tmp_path: Path):
    transport = StubTransport(_payload(("2026-08-19", ["2026-08-19T10:20:00.000+02:00"])))
    await _fetcher(tmp_path, transport).fetch()
    files = list((tmp_path / "doctolib").glob("*.json"))
    assert files
    assert json.loads(files[0].read_text(encoding="utf-8"))["total"] == 1


async def test_a_200_carrying_an_error_key_raises(tmp_path: Path):
    """Doctolib answers 200 with {"error": [...]} for a bad parameter. Reading that as
    an empty calendar is the failure this whole module is shaped to avoid."""
    transport = StubTransport({"error": ["agenda_ids: is missing"]})
    with pytest.raises(DoctolibError, match="agenda_ids"):
        await _fetcher(tmp_path, transport).fetch()


async def test_a_transport_failure_surfaces_as_doctolib_error(tmp_path: Path):
    transport = StubTransport(DoctolibError("Doctolib is rate-limiting. Try again later."))
    with pytest.raises(DoctolibError, match="rate-limiting"):
        await _fetcher(tmp_path, transport).fetch()
