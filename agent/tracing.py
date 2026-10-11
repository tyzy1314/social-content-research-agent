"""Pluggable trace sinks for the agent runtime (no OTel dependency).

Spans are simply events with ``role="trace"``; taxonomy kinds: ``provider_call``,
``tool_invoke``, ``gate_check``, ``approval``, ``checkpoint`` (openai-agents pattern:
the sink interface is the extension point, the transport is swappable).
"""
# allow: SIZE — contract-mandated sinks in one module; implemented after state.py, verified incrementally.

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol, Sequence

from agent.state import AgentStore
from agent.types import Event


class TraceSink(Protocol):
    """Receives trace events emitted by the runtime and providers."""

    def emit(self, event: Event) -> None:
        """Record one trace event."""
        ...

    def close(self) -> None:
        """Release any resources held by the sink."""
        ...


class SqliteTraceSink:
    """Write trace events into ``agent_events`` via an :class:`AgentStore` (default sink).

    The store owns the connection lifecycle, so :meth:`close` is a no-op. Events are
    appended with ``role="trace"`` so ``AgentStore.messages`` excludes them from the
    model context.

    Args:
        store: Store that owns the target database.
        run_id: Run the trace events belong to.
    """

    def __init__(self, store: AgentStore, *, run_id: str) -> None:
        self._store = store
        self._run_id = run_id

    def emit(self, event: Event) -> None:
        """Append the event to ``agent_events`` with ``role="trace"``."""
        self._store.append_event(
            self._run_id,
            step=event.step,
            role="trace",
            kind=event.kind,
            payload=event.payload,
        )

    def close(self) -> None:
        """No-op: the AgentStore's connection is owned by its caller."""


class JsonlTraceSink:
    """Append one JSON line per event to ``data/agent_traces/{run_id}.jsonl``.

    Directories are created on first use. Each line holds the full event record
    (``seq``/``ts``/``step``/``role``/``kind``/``payload``).

    Args:
        run_id: Run the trace events belong to; becomes the file name.
        base_dir: Directory that holds per-run JSONL files.
    """

    def __init__(self, run_id: str, *, base_dir: str | Path = "data/agent_traces") -> None:
        self._path = Path(base_dir) / f"{run_id}.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self._path.open("a", encoding="utf-8")

    @property
    def path(self) -> Path:
        """Path of the JSONL file this sink appends to."""
        return self._path

    def emit(self, event: Event) -> None:
        """Serialise the event as one JSON line and flush it to disk.

        Non-JSON-native payload values are stringified (``default=str``) so a trace
        write can never take a run down.
        """
        record = {
            "seq": event.seq,
            "ts": event.ts,
            "step": event.step,
            "role": event.role,
            "kind": event.kind,
            "payload": dict(event.payload),
        }
        self._file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._file.flush()

    def close(self) -> None:
        """Close the underlying file (idempotent)."""
        if not self._file.closed:
            self._file.close()


class MultiTraceSink:
    """Fan-out sink that forwards every event to each wrapped sink.

    Args:
        sinks: Sinks to forward to, in order.
    """

    def __init__(self, sinks: Sequence[TraceSink]) -> None:
        self._sinks = tuple(sinks)

    def emit(self, event: Event) -> None:
        """Forward the event to every wrapped sink."""
        for sink in self._sinks:
            sink.emit(event)

    def close(self) -> None:
        """Close every wrapped sink (continues on error, re-raises the first failure)."""
        first_error: BaseException | None = None
        for sink in self._sinks:
            try:
                sink.close()
            except BaseException as exc:  # noqa: BLE001 - collect and re-raise the first
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error
