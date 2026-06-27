from __future__ import annotations

from datetime import UTC, datetime, timedelta


def _parse_iso8601_z(ts: str) -> datetime:
    # Accept "Z" or "+00:00".
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


def _format_iso8601_z(dt: datetime) -> str:
    dt = dt.astimezone(UTC).replace(microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")


def next_deterministic_ts(spec_draft: dict) -> str:
    """Return a deterministic, monotonic timestamp for this spec draft.

    The server is stateless; determinism comes from updating `_interview._counter`
    inside the host-owned `spec_draft`.
    """
    meta = spec_draft.setdefault("meta", {})
    created_at = meta.get("created_at") or "1970-01-01T00:00:00Z"
    base = _parse_iso8601_z(created_at)

    interview = spec_draft.setdefault("_interview", {})
    counter = interview.get("_counter")
    if not isinstance(counter, int) or counter < 0:
        counter = 0
    counter += 1
    interview["_counter"] = counter

    return _format_iso8601_z(base + timedelta(seconds=counter))
