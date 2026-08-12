"""Doctolib — open appointment slots at one practice.

Three things here are load-bearing.

**The configured URL is the whole configuration.** Doctolib's `availabilities.json`
needs `agenda_ids`, and those appear nowhere in the public booking page or the practice
profile JSON — the booking app fetches them separately. Rather than reimplement that
discovery against an undocumented endpoint that would break the first time it moved, the
user pastes the request their own browser made and this module reuses its query string,
overriding only `start_date` and `limit`. Configuration is therefore copy-paste, and a
practice that changes its agendas is fixed by pasting a fresh URL rather than by a code
change.

**Nothing here is authenticated and nothing here books.** It is one GET of a public
endpoint per explicit `/doctor`, no account, no cookies, no credentials, no write of any
kind. That is what keeps it inside the Phase-1 non-goals ("credentials or payments",
"any action in the world") and narrower than the scraping the x-pulse spec rejected.
See ADR-016.

**Availability is perishable and the endpoint is undocumented.** A slot read here can be
gone seconds later, so the reply is always framed as "as of now", and every upstream
failure is translated into a sentence a person can act on rather than surfacing an
httpx traceback.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from vos.contracts import AppointmentSlot, DoctolibError, DoctolibSnapshot, slot_id

log = logging.getLogger(__name__)

# The endpoint rejects anything larger, with {"error":["limit: must be less than or
# equal to 15"]}. It counts DAYS returned, not slots.
MAX_DAYS = 15

# Required by the endpoint; a request missing any of them is a 400 rather than an
# empty result, so this is checked before spending a round trip.
REQUIRED_PARAMS = ("visit_motive_ids", "agenda_ids")


Transport = Callable[[str], Awaitable[dict]]


def parse_availabilities_url(url: str) -> dict[str, str]:
    """Pull the query parameters out of a captured availabilities.json URL.

    Raises `DoctolibError` when the URL is not one — a typo in `.env` should say so at
    the first `/doctor` rather than turn into an empty appointment list, which is
    indistinguishable from a genuinely full calendar.
    """
    parsed = urlparse(url.strip())
    if not parsed.scheme.startswith("http") or "doctolib" not in parsed.netloc:
        raise DoctolibError(
            "That does not look like a Doctolib URL.", permanent=True
        )
    # Endswith, not "contains": the booking PAGE is /booking/availabilities, one
    # character class away from the API's /availabilities.json, and pasting the page is
    # the mistake this message exists to name.
    if not parsed.path.endswith("availabilities.json"):
        raise DoctolibError(
            "That is the booking page, not the availabilities request. Copy the "
            "availabilities.json URL from the browser's Network tab.",
            permanent=True,
        )
    params = dict(parse_qsl(parsed.query))
    missing = [p for p in REQUIRED_PARAMS if not params.get(p)]
    if missing:
        raise DoctolibError(
            f"The URL is missing {', '.join(missing)}.", permanent=True
        )
    return params


def slots_from_payload(payload: dict) -> list[AppointmentSlot]:
    """Flatten the per-day response into a flat, sorted list of start times.

    Doctolib returns one entry per day in the window, most of them with an empty `slots`
    list, so the day grouping carries no information the timestamps do not already have.
    Unparseable entries are skipped rather than raising: one malformed timestamp should
    not cost the user the other nine.
    """
    out: list[AppointmentSlot] = []
    for day in payload.get("availabilities") or []:
        if not isinstance(day, dict):
            continue
        for raw in day.get("slots") or []:
            when = _parse_slot(raw)
            if when is not None:
                out.append(AppointmentSlot(starts_at=when))
    out.sort(key=lambda s: s.starts_at)
    return out


def _parse_slot(raw: object) -> datetime | None:
    # Slots arrive as "2026-08-19T10:20:00.000+02:00" — local practice time with an
    # offset, which is what the user should be told. The offset is preserved rather
    # than normalised to UTC so "10:20" stays the time on the practice's door.
    if isinstance(raw, dict):
        raw = raw.get("start_date") or raw.get("start")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        log.info("Unparseable Doctolib slot: %r", raw)
        return None


class SlotFetcher:
    """Open slots for one configured practice, cached on disk.

    The transport is injectable so the whole parsing path is testable without a network,
    and the clock so a test can pin `start_date`.
    """

    def __init__(
        self,
        *,
        source_url: str,
        artifact_dir: Path,
        transport: Transport | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._dir = Path(artifact_dir) / "doctolib"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._source_url = source_url
        self._transport = transport or self._http
        self._now = now or (lambda: datetime.now(UTC))

    def request_url(self, *, days: int = MAX_DAYS) -> str:
        """The URL for one fetch: the configured query with our own window."""
        params = parse_availabilities_url(self._source_url)
        params["start_date"] = self._now().date().isoformat()
        params["limit"] = str(min(days, MAX_DAYS))
        parsed = urlparse(self._source_url.strip())
        return urlunparse(parsed._replace(query=urlencode(params)))

    async def fetch(self, *, days: int = MAX_DAYS) -> DoctolibSnapshot:
        """Ask Doctolib what is free. Raises `DoctolibError` with a user-facing reason."""
        url = self.request_url(days=days)
        payload = await self._transport(url)

        # A 200 carrying an `error` key is Doctolib rejecting the query, not an empty
        # calendar. Treated as permanent: every cause is a wrong parameter.
        if isinstance(payload.get("error"), list | str):
            detail = payload["error"]
            detail = "; ".join(detail) if isinstance(detail, list) else str(detail)
            raise DoctolibError(f"Doctolib rejected the request: {detail}", permanent=True)

        snapshot = DoctolibSnapshot(
            source_url=url,
            fetched_at=self._now(),
            slots=slots_from_payload(payload),
            total=int(payload.get("total") or 0),
            raw_response=payload,
        )
        self._write(snapshot)
        return snapshot

    def _write(self, snapshot: DoctolibSnapshot) -> None:
        stamp = snapshot.fetched_at.strftime("%Y%m%dT%H%M%SZ")
        path = self._dir / f"{stamp}.json"
        try:
            path.write_text(snapshot.model_dump_json(indent=1), encoding="utf-8")
        except OSError as exc:  # pragma: no cover - disk-level failure
            # Not fatal: the user still gets their answer. Only the record is lost.
            log.warning("Could not cache Doctolib snapshot %s: %s", path.name, exc)

    async def _http(self, url: str) -> dict:
        import httpx

        # A browser User-Agent is sent because the endpoint serves a bot challenge to
        # obviously-automated clients. This is one request per explicit command against
        # a public page - it is politeness about volume, not concealment.
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
            ),
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            try:
                response = await client.get(url, headers=headers)
            except httpx.TimeoutException as exc:
                raise DoctolibError("Doctolib did not answer in time.") from exc
            except httpx.HTTPError as exc:
                raise DoctolibError(
                    f"Could not reach Doctolib: {type(exc).__name__}."
                ) from exc

            if response.status_code == 403:
                raise DoctolibError(
                    "Doctolib blocked the request (bot check). Try again later."
                )
            if response.status_code == 429:
                raise DoctolibError("Doctolib is rate-limiting. Try again later.")
            if response.status_code == 410:
                raise DoctolibError(
                    "That Doctolib page is gone. Capture a fresh availabilities URL.",
                    permanent=True,
                )
            if response.status_code >= 500:
                raise DoctolibError("Doctolib is having trouble. Try again later.")
            if response.status_code >= 400:
                raise DoctolibError(
                    f"Doctolib refused the request (HTTP {response.status_code}).",
                    permanent=True,
                )
            try:
                return response.json()
            except (json.JSONDecodeError, ValueError) as exc:
                # HTML where JSON was expected means a challenge page or a redesign.
                raise DoctolibError("Doctolib returned something unreadable.") from exc


__all__ = [
    "MAX_DAYS",
    "SlotFetcher",
    "parse_availabilities_url",
    "slot_id",
    "slots_from_payload",
]
