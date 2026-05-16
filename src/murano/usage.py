"""Token usage tracker.

Every Venice call (embed / chat / summarize) appends a one-line JSON event
to `~/.murano/logs/usage.jsonl`. `murano usage` aggregates that file into
daily and per-model totals.

We deliberately store raw token counts only — pricing changes and Venice
exposes per-model rates via `/v1/models`, so cost estimation can be
applied at display time without rewriting historical data.

The file is append-only and the writer is best-effort: if logging fails
we swallow the error rather than break the actual Venice call.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

LOG_FILENAME = "usage.jsonl"

_logger = logging.getLogger("murano.usage")


@dataclass
class UsageEvent:
    """One observed Venice request. Token counts come straight from the API response."""

    operation: str  # "embed" | "chat" | "summarize" | other
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    timestamp: float = field(default_factory=time.time)
    elapsed_ms: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _log_path(data_root: Path) -> Path:
    return data_root / "logs" / LOG_FILENAME


def log_usage(data_root: Path, event: UsageEvent) -> None:
    """Append `event` to usage.jsonl. Never raises — failures go to the logger."""
    path = _log_path(data_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError) as e:  # pragma: no cover
        _logger.warning("Failed to log usage event: %s", e)


def extract_usage_from_response(resp: Any) -> tuple[int, int, int]:
    """Best-effort extraction of (prompt, completion, total) from an SDK response.

    Handles both chat (`response.usage.prompt_tokens` etc.) and embedding
    (`response.usage.prompt_tokens`, with completion_tokens==0). Returns
    (0, 0, 0) when the response carries no usage info, e.g. a stream chunk
    without `include_usage`.
    """
    usage = getattr(resp, "usage", None)
    if usage is None:
        return 0, 0, 0
    p = int(getattr(usage, "prompt_tokens", 0) or 0)
    c = int(getattr(usage, "completion_tokens", 0) or 0)
    t = int(getattr(usage, "total_tokens", 0) or 0) or (p + c)
    return p, c, t


def iter_usage(data_root: Path) -> Iterator[UsageEvent]:
    """Yield every UsageEvent in the log, oldest first. Skips malformed lines."""
    path = _log_path(data_root)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                yield UsageEvent(
                    operation=str(d.get("operation", "?")),
                    model=str(d.get("model", "?")),
                    prompt_tokens=int(d.get("prompt_tokens", 0) or 0),
                    completion_tokens=int(d.get("completion_tokens", 0) or 0),
                    total_tokens=int(d.get("total_tokens", 0) or 0),
                    timestamp=float(d.get("timestamp", 0.0) or 0.0),
                    elapsed_ms=(
                        float(d["elapsed_ms"]) if d.get("elapsed_ms") is not None else None
                    ),
                    extra=dict(d.get("extra", {})),
                )
            except (json.JSONDecodeError, TypeError, ValueError):
                continue


@dataclass
class UsageSummary:
    total_events: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    by_operation: dict[str, dict[str, int]] = field(default_factory=dict)
    by_model: dict[str, dict[str, int]] = field(default_factory=dict)
    by_day: dict[str, dict[str, int]] = field(default_factory=dict)


def summarize(events: Iterator[UsageEvent] | list[UsageEvent]) -> UsageSummary:
    """Aggregate usage events into totals broken down by operation/model/day."""
    summary = UsageSummary()
    for ev in events:
        summary.total_events += 1
        summary.total_prompt_tokens += ev.prompt_tokens
        summary.total_completion_tokens += ev.completion_tokens
        summary.total_tokens += ev.total_tokens

        for bucket, key in (
            (summary.by_operation, ev.operation),
            (summary.by_model, ev.model),
            (summary.by_day, _day_key(ev.timestamp)),
        ):
            row = bucket.setdefault(
                key,
                {"events": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            )
            row["events"] += 1
            row["prompt_tokens"] += ev.prompt_tokens
            row["completion_tokens"] += ev.completion_tokens
            row["total_tokens"] += ev.total_tokens
    return summary


def _day_key(ts: float) -> str:
    import datetime as _dt

    if ts <= 0:
        return "unknown"
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
