"""Single source of wall-clock time for the whole package.

Every time-dependent decision in lele — a provider reach limit, a window
boundary, a "not in the future" refusal, a control-window floor — must be
reproducible from a recorded input. Reading the system clock deep inside a
function makes that impossible: the same call with the same stored data returns
different answers tomorrow, and a test written against a fixed date silently
changes meaning when that date is reached.

So there is exactly one clock, here. `now()` returns an aware UTC datetime.
`freeze()` substitutes a fixed instant for the duration of a block, which is how
tests pin a boundary. `LELE_NOW` freezes the process at an explicit instant
for a script that must be byte-reproducible.

Nothing else in the package may call `datetime.now()` directly.
"""
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, UTC

__all__ = ["freeze", "frozen_at", "is_frozen", "now"]

_INSTANT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?"
    r"(Z|[+-]\d{2}:?\d{2})?$"
)

_frozen: datetime | None = None


def _parse(value: str) -> datetime:
    text = value.strip()
    if not _INSTANT_RE.match(text):
        raise ValueError(
            "LELE_NOW must be an ISO-8601 instant such as 2026-01-31T12:00:00+00:00"
        )
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"LELE_NOW is not a valid instant: {value!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _environment() -> datetime | None:
    raw = os.environ.get("LELE_NOW") or os.environ.get("FINWORLD_NOW")
    if not raw or not raw.strip():
        return None
    return _parse(raw)


def now() -> datetime:
    """Current instant as an aware UTC datetime, or the frozen instant."""
    if _frozen is not None:
        return _frozen
    pinned = _environment()
    if pinned is not None:
        return pinned
    return datetime.now(UTC)


def frozen_at() -> datetime | None:
    """The instant currently substituted for the clock, or None."""
    if _frozen is not None:
        return _frozen
    return _environment()


def is_frozen() -> bool:
    return frozen_at() is not None


@contextmanager
def freeze(moment: datetime | str) -> Iterator[datetime]:
    """Substitute `moment` for the clock inside the block.

    `moment` may be a datetime or an ISO string. The block is restored even if
    it raises, so a failing test cannot leave the process pinned to a past date
    and make every later test fail for the wrong reason.
    """
    global _frozen
    if isinstance(moment, str):
        moment = _parse(moment)
    if not isinstance(moment, datetime):
        raise TypeError("freeze() requires a datetime or an ISO-8601 string")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    previous = _frozen
    _frozen = moment.astimezone(UTC)
    try:
        yield _frozen
    finally:
        _frozen = previous


def advance(moment: datetime, seconds: int | float) -> datetime:
    """Move an instant forward, for callers that need a relative boundary."""
    return moment + timedelta(seconds=seconds)
