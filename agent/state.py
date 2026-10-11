"""SQLite-backed append-only event store and derived run state for the agent runtime.

The event log in ``agent_events`` is the source of truth (OpenHands pattern); the
``agent_runs`` row holds derived run state and ``agent_tool_budget`` holds per-run,
per-tool invocation counters. Schema DDL is the frozen contract from
``docs/agent/architecture.md`` and is executed with ``CREATE TABLE IF NOT EXISTS`` on init.
"""
# allow: SIZE — frozen contract fixes this module's API/layout; built as sequential per-module subtasks, each ruff+pytest verified.

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from agent.types import Event

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_runs (
  run_id TEXT PRIMARY KEY,
  task TEXT NOT NULL,
  status TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  max_steps INTEGER NOT NULL,
  steps_used INTEGER NOT NULL DEFAULT 0,
  tool_calls INTEGER NOT NULL DEFAULT 0,
  final_text TEXT,
  error TEXT
);
CREATE TABLE IF NOT EXISTS agent_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  ts TEXT NOT NULL,
  step INTEGER NOT NULL,
  role TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  UNIQUE (run_id, seq)
);
CREATE TABLE IF NOT EXISTS agent_tool_budget (
  run_id TEXT NOT NULL,
  tool TEXT NOT NULL,
  used INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, tool)
);
"""

#: Columns of ``agent_runs`` that ``update_run`` may write (run_id excluded on purpose —
#: these names are interpolated into SQL, so they are validated against a frozen set).
_RUN_FIELDS = frozenset(
    {
        "task",
        "status",
        "provider",
        "model",
        "created_at",
        "updated_at",
        "max_steps",
        "steps_used",
        "tool_calls",
        "final_text",
        "error",
    }
)


def _utc_now() -> str:
    """Return the current UTC time as a microsecond-precision ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _flush_tool_calls(messages: list[dict[str, Any]], pending: list[dict[str, Any]]) -> None:
    """Append one grouped assistant message for pending decisions, then clear them."""
    if pending:
        messages.append({"role": "assistant", "tool_calls": pending.copy()})
        pending.clear()


def _tool_result_content(payload: Mapping[str, Any]) -> str:
    """Extract the model-visible content of a tool result from its event payload.

    Primary key is ``content``; falls back to ``value`` then ``error`` (the
    ``ToolResult`` field names) so any of the plausible runtime payload shapes
    round-trips into a valid OpenAI tool message. Non-string values are JSON-encoded.
    """
    content = payload.get("content")
    if content is None:
        content = payload.get("value")
    if content is None:
        content = payload.get("error")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


class AgentStore:
    """Append-only event log plus derived run rows in a single SQLite file.

    All multi-statement writes run inside ``BEGIN IMMEDIATE`` transactions so that
    per-run sequence numbers and budget counters stay atomic under concurrency.

    Args:
        db_path: Path to the SQLite file. Defaults to ``data/agent_runs.db`` per the
            architecture contract; parent directories are created when missing.
    """

    def __init__(self, db_path: str | Path = "data/agent_runs.db") -> None:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.isolation_level = None  # explicit BEGIN IMMEDIATE/COMMIT control
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Run a block inside a ``BEGIN IMMEDIATE`` transaction, committing on success."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def create_run(
        self,
        *,
        run_id: str,
        task: str,
        provider: str,
        model: str | None,
        max_steps: int,
    ) -> None:
        """Insert a new run row in ``running`` state.

        Args:
            run_id: Unique run identifier.
            task: The user task text.
            provider: Provider name (e.g. ``scripted``).
            model: Model identifier, if the provider has one.
            max_steps: Step budget for the run.

        Raises:
            sqlite3.IntegrityError: A run with this ``run_id`` already exists.
        """
        now = _utc_now()
        self._conn.execute(
            "INSERT INTO agent_runs (run_id, task, status, provider, model, created_at,"
            " updated_at, max_steps, steps_used, tool_calls, final_text, error)"
            " VALUES (?, ?, 'running', ?, ?, ?, ?, ?, 0, 0, NULL, NULL)",
            (run_id, task, provider, model, now, now, max_steps),
        )

    def append_event(
        self,
        run_id: str,
        *,
        step: int,
        role: str,
        kind: str,
        payload: Mapping[str, Any],
    ) -> Event:
        """Append one event, assigning ``seq = max(seq) + 1`` for this run atomically.

        The payload is stored as ``json.dumps(..., ensure_ascii=False, sort_keys=True)``
        so storage is byte-stable regardless of dict insertion order.

        Args:
            run_id: Run the event belongs to.
            step: Runtime step at which the event occurred.
            role: Event role (``system``/``user``/``assistant``/``tool``/``trace``).
            kind: Event taxonomy kind (``decision``/``tool_result``/``final``/...).
            payload: JSON-serialisable event payload.

        Returns:
            The stored event with its assigned ``seq`` and timestamp.
        """
        ts = _utc_now()
        encoded = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)
        with self._transaction():
            row = self._conn.execute(
                "SELECT MAX(seq) AS max_seq FROM agent_events WHERE run_id = ?", (run_id,)
            ).fetchone()
            seq = int(row["max_seq"] or 0) + 1
            self._conn.execute(
                "INSERT INTO agent_events (run_id, seq, ts, step, role, kind, payload)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, seq, ts, step, role, kind, encoded),
            )
        return Event(  # type: ignore[arg-type]
            seq=seq, ts=ts, step=step, role=role, kind=kind, payload=json.loads(encoded)
        )

    def events(self, run_id: str) -> list[Event]:
        """Return every event of a run in ascending ``seq`` order.

        Args:
            run_id: Run to read.

        Returns:
            Events with payloads decoded from JSON.
        """
        rows = self._conn.execute(
            "SELECT seq, ts, step, role, kind, payload FROM agent_events"
            " WHERE run_id = ? ORDER BY seq",
            (run_id,),
        ).fetchall()
        return [
            Event(
                seq=int(row["seq"]),
                ts=row["ts"],
                step=int(row["step"]),
                role=row["role"],  # type: ignore[arg-type]
                kind=row["kind"],
                payload=json.loads(row["payload"]),
            )
            for row in rows
        ]

    def messages(self, run_id: str) -> list[dict[str, Any]]:
        """Rebuild the model message list from the event log (OpenHands pattern).

        State is derived; the event log is the source of truth. Mapping rules:

        * a user-kind event (``role="user"`` or ``kind in {"user", "task"}``) carrying a
          ``task`` payload becomes ``{"role": "user", "content": task}``;
        * consecutive ``kind="decision"`` events carrying ``tool_call_id``/``tool``/``args``
          are grouped into ONE assistant message whose ``tool_calls`` entries are
          ``{"id", "type": "function", "function": {"name", "arguments"}}``;
        * ``kind="tool_result"`` becomes ``{"role": "tool", "tool_call_id", "content"}``
          (payload fallbacks: ``call_id`` for the id; ``value``/``error`` for the content);
        * ``kind="final"`` becomes ``{"role": "assistant", "content": final_text}``
          (payload fallback: ``final`` for ``final_text``);
        * ``role="trace"`` events are excluded entirely (they never split a decision
          group); other kinds such as ``error``/``checkpoint`` are ignored but do end a
          run of consecutive decisions.

        Args:
            run_id: Run whose event log is replayed.

        Returns:
            OpenAI-style message list in event order.
        """
        messages: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        for event in self.events(run_id):
            if event.role == "trace":
                continue
            payload = event.payload
            if event.kind == "decision" and "tool" in payload and "tool_call_id" in payload:
                pending.append(
                    {
                        "id": payload["tool_call_id"],
                        "type": "function",
                        "function": {
                            "name": payload["tool"],
                            "arguments": json.dumps(payload.get("args", {})),
                        },
                    }
                )
                continue
            _flush_tool_calls(messages, pending)
            if "task" in payload and (event.role == "user" or event.kind in ("user", "task")):
                messages.append({"role": "user", "content": payload["task"]})
            elif event.kind == "tool_result" and ("tool_call_id" in payload or "call_id" in payload):
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": payload.get("tool_call_id", payload.get("call_id")),
                        "content": _tool_result_content(payload),
                    }
                )
            elif event.kind == "final":
                text = payload.get("final_text")
                if text is None:
                    text = payload.get("final", "")
                messages.append({"role": "assistant", "content": text})
        _flush_tool_calls(messages, pending)
        return messages

    def update_run(self, run_id: str, **fields: Any) -> None:
        """Update derived fields of a run row and bump ``updated_at``.

        Args:
            run_id: Run to update.
            **fields: Column name/value pairs from the validated ``agent_runs`` field set.

        Raises:
            ValueError: No fields given, or a field name is not an ``agent_runs`` column
                (field names are interpolated into SQL, so unknown names are rejected).
            KeyError: No run with this ``run_id`` exists.
        """
        if not fields:
            raise ValueError("update_run requires at least one field")
        unknown = set(fields) - _RUN_FIELDS
        if unknown:
            raise ValueError(f"unknown agent_runs field(s): {', '.join(sorted(unknown))}")
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self._transaction():
            cursor = self._conn.execute(
                f"UPDATE agent_runs SET {assignments}, updated_at = ? WHERE run_id = ?",
                (*fields.values(), _utc_now(), run_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"unknown run: {run_id}")

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Return the run row as a dict, or ``None`` when the run does not exist."""
        row = self._conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row is not None else None

    def budget_get(self, run_id: str, tool: str) -> int:
        """Return how many times ``tool`` was invoked in ``run_id`` (0 when never)."""
        row = self._conn.execute(
            "SELECT used FROM agent_tool_budget WHERE run_id = ? AND tool = ?",
            (run_id, tool),
        ).fetchone()
        return int(row["used"]) if row is not None else 0

    def budget_bump(self, run_id: str, tool: str) -> int:
        """Atomically increment a per-tool budget and return the new count.

        The upsert and read-back run inside one ``BEGIN IMMEDIATE`` transaction, so
        concurrent bumps never lose an increment: the first call returns 1, the second 2.

        Args:
            run_id: Run the budget belongs to.
            tool: Tool name.

        Returns:
            The budget count after the increment.
        """
        with self._transaction():
            self._conn.execute(
                "INSERT INTO agent_tool_budget (run_id, tool, used) VALUES (?, ?, 1)"
                " ON CONFLICT(run_id, tool) DO UPDATE SET used = used + 1",
                (run_id, tool),
            )
            row = self._conn.execute(
                "SELECT used FROM agent_tool_budget WHERE run_id = ? AND tool = ?",
                (run_id, tool),
            ).fetchone()
        return int(row["used"])

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return run rows newest first (``created_at`` descending, insert order as tiebreak)."""
        rows = self._conn.execute(
            "SELECT * FROM agent_runs ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
