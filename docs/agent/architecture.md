# Agent Architecture — Frozen Contracts

Status: **FROZEN**. Implementation must match these signatures exactly. Any deviation requires
updating this file first, then the code.

## Why this shape

`xiaohongshu-sentiment-analysis` is a fixed-workflow CLI crawler/analysis tool. It had no
model-driven loop, no typed tool registry, no execution trace, and an allowlist permission
model (`agent_permissions.py`) whose `capability_allowed` / `output_path_allowed` had **no
runtime call sites**. The design below adds exactly the missing runtime pieces and nothing
else — no multi-agent, no vector memory, no OTel farm (business determines architecture;
see `docs/agent/github-comparison.md` for the source-level rationale).

Patterns adopted (source-cited in the comparison doc): typed next-step ADT (openai-agents),
three-outcome tool errors (pydantic-ai), per-tool budgets + arg-hash-bound approval
(microsoft/agent-framework), append-only event log with derived state (OpenHands),
iteration/budget ControlFlag + stuck detector (OpenHands), pluggable trace sink
(openai-agents).

## Package layout

```
agent/
  __init__.py       # public re-exports
  types.py          # Decision, ToolCall, ToolResult, ModelTurn, RunResult, Event
  errors.py         # exception hierarchy
  tools.py          # ToolSpec, ToolRegistry, schema generation, invocation
  permissions.py    # PermissionGate (wraps existing agent_permissions.py)
  providers.py      # ModelProvider ABC + 3 implementations
  state.py          # AgentStore (SQLite): append-only events + derived run row
  tracing.py        # TraceSink protocol + JsonlTraceSink / SqliteTraceSink
  runtime.py        # AgentRuntime — the loop
  cli.py            # argument parsing / rendering for `main.py agent`
```

## `types.py`

```python
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence

DecisionKind = Literal["call_tool", "finish"]
ErrorKind = Literal["retryable", "invalid_input", "failed"]
RunStatus = Literal["running", "finished", "failed", "blocked", "exceeded", "stuck", "cancelled"]

@dataclass(frozen=True)
class ToolCall:
    tool: str
    args: Mapping[str, Any]
    call_id: str                      # stable id: provider-native, else f"call_{seq}"

@dataclass(frozen=True)
class ToolResult:
    call_id: str
    tool: str
    ok: bool
    value: Any = None                 # JSON-serialisable on success
    error: str | None = None          # model-visible, no tracebacks/paths leaking
    error_kind: ErrorKind | None = None
    duration_ms: int = 0
    attempts: int = 1

@dataclass(frozen=True)
class ModelTurn:
    """Normalised provider output. Providers never return raw SDK objects."""
    tool_calls: tuple[ToolCall, ...] = ()
    final_text: str | None = None
    finish_reason: str | None = None
    usage: Mapping[str, int] = field(default_factory=dict)   # prompt/completion/total tokens
    raw: Any = None                                       # provider-specific, for tracing only

@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    tool: str | None = None
    args: Mapping[str, Any] = field(default_factory=dict)
    final: str | None = None
    source: Literal["model", "scripted", "forced"] = "model"

@dataclass(frozen=True)
class Event:
    """One append-only row in agent_events."""
    seq: int
    ts: str                          # UTC ISO-8601
    step: int
    role: Literal["system", "user", "assistant", "tool", "trace"]
    kind: str                        # decision|tool_call|tool_result|error|checkpoint|final
    payload: Mapping[str, Any]

@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: RunStatus
    final_text: str | None
    steps_used: int
    tool_calls: int
    usage: Mapping[str, int]
    error: str | None = None
    events: tuple[Event, ...] = ()
```

## `errors.py`

```python
class AgentError(Exception): ...
class ModelError(AgentError): ...                 # provider transport/protocol failure
class ModelProtocolError(ModelError): ...         # non-JSON, unknown action, contamination
class ToolError(AgentError):
    def __init__(self, message: str, *, kind: ErrorKind = "failed"): ...
class ToolNotFound(ToolError):                    # kind defaults to "invalid_input"
class ToolValidationError(ToolError):             # kind="invalid_input" — schema mismatch
class ToolFailed(ToolError):                      # kind="failed" — handler raised
class ModelRetry(ToolError):                      # kind="retryable" — hint for the model
class PermissionDenied(AgentError): ...
class ApprovalRequired(AgentError):
    def __init__(self, tool: str, args: Mapping[str, Any], call_id: str): ...
class MaxStepsExceeded(AgentError): ...
class StuckDetected(AgentError): ...
class BudgetExceeded(AgentError): ...
class ProviderNotAvailable(AgentError): ...
```

Rule (pydantic-ai pattern): a tool handler may raise `ModelRetry("message")` to feed a
retryable hint back to the model; `ToolValidationError` for bad input; anything else becomes
`ToolFailed` with an **opaque** message ("tool X failed") — never a traceback, never a local
absolute path.

## `tools.py`

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: Mapping[str, Any]          # JSON Schema, {"type":"object","properties":...}
    handler: Callable[..., Any]
    mutates: bool = False                  # side-effecting? drives approval policy
    requires_approval: bool = False        # explicit override; defaults to `mutates`
    max_invocations: int | None = None     # per-run budget (agent-framework pattern)
    timeout_seconds: float | None = None
    tags: tuple[str, ...] = ()             # e.g. ("read",) / ("db","write")

class ToolRegistry:
    def register(self, spec: ToolSpec) -> None: ...
    def get(self, name: str) -> ToolSpec: ...          # raises ToolNotFound
    def names(self) -> list[str]: ...
    def openai_tools(self) -> list[dict[str, Any]]: ...   # OpenAI `tools` array
    def invoke(self, call: ToolCall, *, gate: "PermissionGate",
               budget: "InvocationBudget", run_id: str, step: int) -> ToolResult: ...

def spec_from_function(fn: Callable[..., Any], *, name: str | None = None,
                       description: str | None = None, mutates: bool = False,
                       requires_approval: bool | None = None,
                       max_invocations: int | None = None,
                       timeout_seconds: float | None = None,
                       tags: Sequence[str] = ()) -> ToolSpec: ...
```

`spec_from_function` builds `parameters` from `inspect.signature` + `typing.get_type_hints`
(stdlib only). Supported parameter types: `str`, `int`, `float`, `bool`, `list`, `dict`,
`list[str]`, `dict[str, str]`, and `Optional[...]` of those. Unsupported types raise at
registration time (fail fast, never at call time).

`invoke` is the **only** path to a handler. It must, in order:
1. budget check (`BudgetExceeded` if `max_invocations` exhausted);
2. `gate.allow_tool(name, args, tags=spec.tags)` (`PermissionDenied`);
3. approval check — if approval required and not pre-approved for this arg-hash →
   `ApprovalRequired` (persisted pending request, loop stops; agent-framework pattern);
4. args validation against `parameters` (`ToolValidationError`);
5. handler call with `timeout_seconds` enforcement;
6. map outcome → `ToolResult` (success value / `ModelRetry` → kind retryable /
   `ToolValidationError` → invalid_input / other → failed+opaque).

## `permissions.py`

Wraps the existing `agent_permissions.py` (keep its `build_permission_audit`,
`capability_allowed`, `output_path_allowed` — do not rewrite them) and adds the missing
runtime enforcement:

```python
class PermissionGate:
    def __init__(self, *, allowed_capabilities: Iterable[str] | None = None,
                 write_roots: Iterable[str] | None = None,
                 allow_all: bool = False): ...
    def allow_tool(self, tool: str, args: Mapping[str, Any], *, tags: Sequence[str]) -> None:
        """Raise PermissionDenied when the tool's tags/capabilities are outside the
        allowlist, or when a path-ish arg resolves outside write_roots."""
    def allow_path(self, path: str) -> None: ...
    def audit(self) -> dict[str, Any]: ...          # serialisable, goes into the trace
```

Mapping: a tool tagged `("read",)` requires capability `read`; `("db","write")` requires
`write`; network egress tags require `network`. Any `*_path` / `path` / `output` / `file`
string arg is resolved and checked with `output_path_allowed`. **This is the call site that
was missing** — every `ToolRegistry.invoke` goes through `gate.allow_tool`.

## `providers.py`

```python
class ModelProvider(ABC):
    name: ClassVar[str]
    @abstractmethod
    def complete(self, *, system: str, messages: Sequence[Mapping[str, Any]],
                 tools: Sequence[Mapping[str, Any]], step: int) -> ModelTurn: ...
    def close(self) -> None: ...                   # default no-op
```

Message format is OpenAI-style dicts: `{"role": ..., "content": ...}` plus
`{"role":"tool","tool_call_id":...,"content":...}` for results. Providers translate as needed.

### `ScriptedProvider` (`name = "scripted"`) — Tier 1, tests only
```python
class ScriptedProvider(ModelProvider):
    def __init__(self, turns: Sequence[ModelTurn]): ...
    def __init__(self, script: Sequence[Mapping[str, Any]]): ...  # dict form, e.g.
        # {"tool": "db_stats", "args": {}} / {"final": "done"}
```
Raises `ModelProtocolError` when the script is exhausted. Never touches the network. Must be
marked in its docstring: **mock only — not evidence of real model behaviour**.

### `OpenAICompatibleProvider` (`name = "openai"`) — Tier 2/3/4 production path
```python
class OpenAICompatibleProvider(ModelProvider):
    def __init__(self, *, base_url: str, api_key: str, model: str,
                 timeout: float = 120.0, max_retries: int = 3): ...
```
stdlib `urllib.request` only (no new deps). POST `{base_url}/chat/completions` with
`tools=`/`tool_choice="auto"`. Translates native `tool_calls[]` into `ToolCall` tuple and
`usage` into the standard three keys. Retries transport errors/429/5xx with exponential
backoff + jitter (langgraph `RetryPolicy` numbers: initial 0.5s, factor 2.0, max 128s,
max_retries 3). 4xx other than 429 → `ModelError`, no retry. Responses that are neither
valid JSON nor parseable → `ModelProtocolError`.

### `OpencodeCliProvider` (`name = "opencode"`) — Tier 3, this environment only
```python
class OpencodeCliProvider(ModelProvider):
    def __init__(self, *, model: str = "opencode/mimo-v2.6-flash-free",
                 workdir: str | None = None, timeout: float = 120.0,
                 max_retries: int = 2): ...
```
Runs `opencode run --pure --format json` as a subprocess. **Hard constraints, all verified
empirical findings from this machine:**
- Runs in an isolated throwaway working directory (default: temp dir containing only a
  minimal `opencode.json`). Its own tools must never see the project tree — a default agent
  in the project root has been observed calling `glob`/`bash` and querying the real
  `data/xhs_crawler.db` (contamination).
- Disabling tools or permissions via `opencode.json` makes the free tier answer
  `403 FreeTierError: "OpenCode's free tier can only be used from within OpenCode"`
  (reproduced with agent-level `tools`, agent-level `permission`, and top-level `tools`).
  Therefore tools cannot be disabled — isolation + sanitisation are the mitigation.
- Any `tool_use` event in the subprocess output = **protocol violation**: the turn is
  rejected, `ModelProtocolError` raised after `max_retries` prompt-strictening attempts.
  Those subprocess tool calls never reach `ToolRegistry`.
- Subprocess is killed on wall-clock timeout (observed: a bash-tempted run blocked >300s on
  an interactive permission prompt).
- Decision contract: the model must emit a single JSON object
  `{"action": "<tool>|FINISH", "args": {...}}`. Parsed leniently (fenced blocks stripped),
  validated strictly (unknown action → protocol error).

## `state.py`

SQLite via stdlib `sqlite3`, one file (default `data/agent_runs.db`, overridable).

```sql
CREATE TABLE IF NOT EXISTS agent_runs (
  run_id TEXT PRIMARY KEY,
  task TEXT NOT NULL,
  status TEXT NOT NULL,             -- RunStatus
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
  payload TEXT NOT NULL,            -- JSON
  UNIQUE (run_id, seq)
);
CREATE TABLE IF NOT EXISTS agent_tool_budget (
  run_id TEXT NOT NULL,
  tool TEXT NOT NULL,
  used INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, tool)
);
```

```python
class AgentStore:
    def __init__(self, db_path: str | Path): ...
    def create_run(self, *, run_id: str, task: str, provider: str, model: str | None,
                   max_steps: int) -> None: ...
    def append_event(self, run_id: str, *, step: int, role: str, kind: str,
                     payload: Mapping[str, Any]) -> Event: ...
    def events(self, run_id: str) -> list[Event]: ...
    def messages(self, run_id: str) -> list[dict[str, Any]]:
        """Rebuild the model message list from the event log (OpenHands pattern:
        state is derived, the event log is the source of truth)."""
    def update_run(self, run_id: str, **fields: Any) -> None: ...
    def get_run(self, run_id: str) -> dict[str, Any] | None: ...
    def budget_get(self, run_id: str, tool: str) -> int: ...
    def budget_bump(self, run_id: str, tool: str) -> int: ...
    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]: ...
```

`messages()` maps: user task → `{"role":"user"}`; decisions → assistant messages carrying
tool_calls; tool results → `{"role":"tool", ...}`; final → assistant text. Trace events are
excluded.

## `tracing.py`

```python
class TraceSink(Protocol):
    def emit(self, event: Event) -> None: ...
    def close(self) -> None: ...

class JsonlTraceSink:    # appends to data/agent_traces/{run_id}.jsonl
class SqliteTraceSink:   # writes into agent_events (the default; zero extra files)
class MultiTraceSink:    # fan-out
```

No hard OTel dependency (openai-agents pattern). Spans are simply events with
`role="trace"`; taxonomy kinds: `provider_call`, `tool_invoke`, `gate_check`, `approval`,
`checkpoint`.

## `runtime.py`

```python
@dataclass
class RuntimeConfig:
    max_steps: int = 10                       # agent-framework / openai-agents default
    stuck_threshold: int = 3                  # identical (tool,args) hash repeats
    stuck_window: int = 6
    approval_policy: Literal["deny_mutations", "allow_all", "ask"] = "deny_mutations"
    system_prompt: str | None = None          # None → built-in default
    max_message_chars: int = 4000             # tool result truncation for the model

class AgentRuntime:
    def __init__(self, *, provider: ModelProvider, registry: ToolRegistry,
                 store: AgentStore, gate: PermissionGate,
                 config: RuntimeConfig | None = None,
                 sink: TraceSink | None = None,
                 approver: Callable[[ToolCall], bool] | None = None): ...
    def run(self, task: str, *, run_id: str | None = None) -> RunResult: ...
    def resume(self, run_id: str) -> RunResult: ...
```

Loop (one iteration = one step):

```
while True:
  step budget check            -> status="exceeded", raise MaxStepsExceeded (recorded)
  messages = store.messages(run_id)
  turn = provider.complete(...)              # ModelError recorded -> status="failed"
  if turn.tool_calls:
      for call in turn.tool_calls:
          record assistant tool_call event
          result = registry.invoke(call, gate=..., budget=..., run_id, step)
                                           # PermissionDenied -> status="blocked"
                                           # ApprovalRequired -> persist + stop ("blocked")
          record tool_result event
      stuck check over last N (tool,args) hashes -> status="stuck"
      continue
  elif turn.final_text is not None:
      record final event; status="finished"; return
  else:
      raise ModelProtocolError (recorded, retried by provider)
```

Every exit path updates `agent_runs` before returning. `resume(run_id)` reloads events,
rebuilds messages, and continues from the stored step counter.

## `cli.py` + `main.py` integration

New subcommand (added to the existing argparse tree in `main.py`, same style as the other six):

```
python main.py agent "TASK" [--provider opencode|openai|scripted] [--max-steps N]
                           [--run-id ID] [--resume] [--yes] [--db PATH] [--json]
```

- `--provider opencode` (default when `ai.enabled` is false and no base_url) →
  `OpencodeCliProvider`.
- `--provider openai` → `OpenAICompatibleProvider` from `ai.base_url` / `ai.api_key` /
  `ai.model` in `config.yaml`.
- `--yes` sets `approval_policy="allow_all"` (headless); default `deny_mutations` refuses
  side-effecting tools and reports them in the final answer instead of executing them.
- `--json` prints the `RunResult` as JSON; default prints a human-readable step trace.

### Tools wired for the first release (real, from existing modules)

| Tool | Source | mutates | Notes |
|---|---|---|---|
| `db_stats` | `db.py` counts | no | read-only |
| `db_query` | read-only SELECT | no | connection opened `mode=ro`, statement must start with SELECT/WITH |
| `db_keywords` | `db.py` keyword list | no | read-only |
| `plan_crawl` | `crawl_planner.plan_crawl` | no | also surfaces the permission audit |
| `discover_keywords` | `discovery.discover_keywords` | yes | approval-gated |
| `run_analysis` | `analysis` entry point | yes | approval-gated |
| `export_data` | `export` entry point | yes | approval-gated + path gate |

No new third-party dependencies. Python 3.10 compatible (project runtime is 3.10.11).

## Evidence tiers (must be reported honestly in `docs/agent/evaluation.md`)

| Tier | What | How it is proven |
|---|---|---|
| 1 — mock | loop, tools, gate, stuck, budget, resume | `ScriptedProvider`, no I/O |
| 2 — local integration | real HTTP, real serialisation, real tool_calls parsing | `OpenAICompatibleProvider` against an in-test `http.server` mock |
| 3 — real model | `OpencodeCliProvider` + live `opencode run` | integration test marked `real_model`, skipped if CLI absent |
| 4 — E2E | `python main.py agent ...` end to end | shell run recorded in `evaluation.md` with artefacts |

A tier-1 pass is never described as "the agent works with a real model". Tier 3/4 results are
reported with wall-clock time, step count, and whether any subprocess `tool_use` contamination
was observed.

## Implementation status (post-build)

The two modules left as "frozen contracts" above are now implemented, and the implementation
matches these signatures **exactly** (no deviations):

- `agent/runtime.py` — `RuntimeConfig` (dataclass) + `AgentRuntime` with `run(task, *, run_id=None)`
  and `resume(run_id)`. The loop follows the pseudocode: step-budget check → `messages =
  store.messages(run_id)` → `provider.complete(...)` → for each `tool_call` invoke through
  `ToolRegistry.invoke` (the six-stage pipeline) and record `decision`/`tool_result` events →
  stuck check → `continue`; `final_text` → `finished`; neither → `failed`. Every exit path calls
  `store.update_run` before returning. `resume` reloads events, rebuilds messages, and continues
  from the stored step counter.
- `agent/cli.py` — `add_agent_parser(subparsers)` + `run_agent(args, config)`. Supports
  `--provider opencode|openai|scripted`, `--max-steps`, `--run-id`, `--resume`, `--yes`
  (`approval_policy="allow_all"`), `--db`, `--json`. Provider construction reads `config.yaml`
  (`ai.base_url` / `ai.api_key` / `ai.model`).
- `main.py` — the `agent` subcommand is registered in `parse_args` and dispatched in `main`.

Validation: `tests/test_agent_runtime.py` (13 tests, Tier 1) and an in-process
`http.server` mock for `OpenAICompatibleProvider` (Tier 2) pass; `python main.py agent`
runs end to end against the real business tools with the run persisted to `data/agent_runs.db`
(Tier 4). Full evidence and honest Tier-3 status are in `docs/agent/evaluation.md`.

