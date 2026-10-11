# GitHub Agent Engineering Source-Level Comparison

Purpose: source-grounded benchmark of six agent engineering repositories against the capability
gaps identified in this repository (see `docs/agent/architecture.md`). Every cell is graded
`VERIFIED` (read from cloned source at a named commit), `PARTIAL`, `MISSING` (negative verified
by search), or `UNVERIFIED` (not inspected).

## Method

- `gh` CLI is **not installed** on this machine. Substituted: shallow/sparse `git clone` +
  GitHub REST API (`api.github.com`, the same backend `gh` uses) + direct file reads.
- All clones were made at the commit SHAs below; line numbers refer to those exact trees.
- Repos whose layout changed were pinned to the ref that actually contains the studied code
  (notably OpenHands — see structural finding #1).

| Repo | Ref inspected | Stars (2026-10-11) | License | Primary language |
|---|---|---|---|---|
| langchain-ai/langgraph | `b4991f1ba36a2449ab59c38ab71828ee865bff84` | 43,033 | MIT | Python |
| openai/openai-agents-python | `125efa029b4bfd84238bd2c4fd69c3406f802663` | 29,954 | MIT | Python |
| crewAIInc/crewAI | `6b93fa0e25a926215ca7bb52a95d4db7d9d7262d` | 59,547 | MIT | Python |
| pydantic/pydantic-ai | `69ea1e57201ed59e7d30fbe4a7590d193acecce0` | 20,537 | MIT | Python |
| OpenHands/OpenHands | main `916c641a872d4fbde56255254eecb4de472a79f4` + tag `0.62.0` (`7fbb48c40679afd674970966b96185657d92a487`) | 90,530 | MIT (enterprise/ separate) | TypeScript (main) |
| microsoft/agent-framework | `fd52de71579162a888fb2dd7510c57c6014dcdfc` | 14,050 | MIT | Python |

## Structural findings (read these before trusting any path reference)

1. **OpenHands/OpenHands has pivoted to TypeScript.** `main` is now "Agent Canvas"
   (npm `@openhands/agent-canvas`): no `openhands/` Python package, no `pyproject.toml`.
   The classic Python runtime (`openhands/controller`, `openhands/events`,
   `openhands/runtime`) is preserved at tag `0.62.0`; its evolution continues in sibling
   repos `OpenHands/legacy` (server remainder, frozen 2026-07-25) and
   `OpenHands/software-agent-sdk` (OpenHands V1 SDK, active). OpenHands cells below are
   sourced from tag `0.62.0` unless stated otherwise.
2. **crewAI moved to a monorepo.** `src/crewai/` no longer exists at HEAD; the package lives
   at `lib/crewai/src/crewai/`, with `lib/crewai-core`, `lib/crewai-tools`, `lib/cli`.
3. **openai-agents loop was factored out.** `src/agents/run.py` (2,679 lines) is now a façade;
   real mechanics live in `src/agents/run_internal/` (`run_loop.py`, `turn_resolution.py`,
   `tool_execution.py`).
4. **langgraph is a monorepo.** Runtime = `libs/langgraph/`; checkpointer base =
   `libs/checkpoint/` (separate PyPI package); tools/`@tool` live in **langchain-core**, not
   this repo.
5. **pydantic-ai is a workspace**: `pydantic_ai_slim/` (core), `pydantic_graph/` (graph engine),
   `pydantic_evals/` (first-class eval package), `clai/`, `docs/`.

## Capability matrix

| Capability | langgraph | openai-agents | crewAI | pydantic-ai | OpenHands | agent-framework |
|---|---|---|---|---|---|---|
| Core agent loop entry point | VERIFIED `pregel/main.py:stream` superstep loop (L3050-3079) | VERIFIED `run.py:_run_impl` L1026 while-True turn loop | VERIFIED `crew.py:kickoff` L995 → `crew_agent_executor.py:_invoke_loop` L331 | VERIFIED `agent/__init__.py:run` → `_agent_graph.py:CallToolsNode` L2213 | VERIFIED `agent_controller.py:_on_event` L477 → `_step` L852 | VERIFIED `_harness/_loop.py:AgentLoopMiddleware.process` L445 |
| Loop result as typed ADT (not ad-hoc flags) | PARTIAL (checkpoint `status` strings: done/out_of_steps) | VERIFIED `run_internal/run_steps.py` `NextStepRunAgain\|FinalOutput\|Interruption\|Handoff` L174-207 | PARTIAL (`AgentAction`/`AgentFinish` parse results only) | VERIFIED graph node returns next-node or `End(FinalResult)` | PARTIAL (event-driven, no turn ADT) | VERIFIED middleware `should_continue` + `MiddlewareTermination` |
| Iteration / turn cap | VERIFIED `DEFAULT_RECURSION_LIMIT=10007` (`_internal/_config.py` L32) + `stop` → `out_of_steps` | VERIFIED `DEFAULT_MAX_TURNS=10` (`run_config.py` L45) → `MaxTurnsExceeded` | VERIFIED `max_iter=25` (`base_agent.py` L286) + graceful `handle_max_iterations_exceeded` | VERIFIED `UsageLimits.request_limit=50` (`usage.py` L485) | VERIFIED `ControlFlag` iteration+budget (`control_flags.py`), default limit 100 | VERIFIED `DEFAULT_MAX_ITERATIONS=10` (`_loop.py` L126) |
| Retry policy (typed, backoff, jitter) | VERIFIED `RetryPolicy` (`types.py` L427-446) + `run_with_retry` (`_retry.py` L573) | VERIFIED `retry.py` predicate policies `network_error`/`retry_after`/`http_status` combinators | PARTIAL (guardrail retries on Task; parser retries) | VERIFIED `AgentRetries{tools,output}` budgets (`agent/abstract.py` L112-132) | PARTIAL (typed LLM error handling in `_step`; no backoff policy object) | MISSING in core (provider clients own retries) |
| Tool error → model feedback | VERIFIED `TOOL_CALL_ERROR_TEMPLATE` + `_default_handle_tool_errors` (`tool_node.py` L119-128, L391) | VERIFIED opaque default message + `failure_error_function` (`tool.py` L1980) | VERIFIED INVALID_INPUT `ToolFailure` message (`tool_utils.py` L257-271) | VERIFIED `ModelRetry`/`ToolRetryError`/`ToolFailurePart` with separate budgets | PARTIAL (`ErrorObservation` re-enters event stream) | VERIFIED typed `_FunctionArgumentValidationError`/`_UnknownFunctionCallError` |
| Tool schema generation from signature | UNVERIFIED (lives in langchain-core) | VERIFIED `function_schema.py` Griffe docstring parsing, `strict_json_schema` | VERIFIED `@tool` builds pydantic `create_model` from type hints (`base_tool.py` L678) | VERIFIED `_function_schema.py` + `GenerateToolJsonSchema` | PARTIAL (hand-written JSON-schema dicts, e.g. `tools/bash.py` L42-80) | VERIFIED `@tool` decorator L1595 + `_validate_arguments_against_schema` L1515 |
| Per-tool invocation budgets | MISSING | PARTIAL (timeout only) | VERIFIED `max_usage_count` claim gate (`base_tool.py`) | VERIFIED retry budget for tools | MISSING | VERIFIED `max_invocations` + `max_invocation_exceptions` (`_tools.py` L510) |
| HITL approval / interruption | VERIFIED `interrupt()` + `Command` resume (`types.py` L833-903), node re-executes | VERIFIED `needs_approval` → `NextStepInterruption` + `RunState.approve()/reject()` | VERIFIED `human_input` + `HumanInputProvider` protocol (`core/providers/human_input.py`) | VERIFIED `ApprovalRequired` exception + `approval_required.py` toolset | VERIFIED `AWAITING_USER_CONFIRMATION` state machine + security analyzer (L950-999) | VERIFIED `ToolApprovalMiddleware` + approval bound to argument tokens (`_tool_approval.py` L104, `_tools.py` L154) |
| Durable state / resume | VERIFIED `BaseCheckpointSaver.put/put_writes` + pending-writes re-apply (`_loop.py` L693) | VERIFIED `RunState` snapshot + `session_persistence.py` | PARTIAL (checkpoint `from_checkpoint`, `output_file`) | PARTIAL (message history caller-owned; durability via adapters) | VERIFIED append-only `EventStream` + `State.__getstate__` drops history (`stream.py`, `state.py` L191) | VERIFIED `FileSessionStore` + per-model-call persistence middleware (`_sessions.py` L1586, L2003) |
| Loop/stuck detection | MISSING (guard is recursion count) | MISSING | MISSING | MISSING | VERIFIED `StuckDetector.is_stuck()` (`stuck.py`) + recovery paths (`agent_controller.py` L1273-1335) | MISSING |
| Memory / context compaction | VERIFIED channel state; no LLM compaction | VERIFIED `Session` protocol + compaction sessions (`memory/session.py` L53) | VERIFIED `unified_memory` + lancedb/qdrant storage (`memory/`) | PARTIAL (history only; `EndStrategy` for tool fan-out) | VERIFIED `CondenserPipeline` chain (`memory/condenser/impl/pipeline.py`) | VERIFIED `ContextWindowCompactionStrategy` before/after model call (`_compaction.py`) |
| Guardrails (input/output/tripwire) | MISSING in core | VERIFIED `InputGuardrail`/`OutputGuardrail` + tripwire exceptions (`guardrail.py` L72/L134) | VERIFIED guardrail retries + `tool_hooks` | VERIFIED `output_type` validation + `@agent.output_validator` L2575 | PARTIAL (security analyzers as pre-execution gates) | VERIFIED label/policy enforcement (`security.py` `PolicyEnforcementFunctionMiddleware` L2650) |
| Observability | VERIFIED LangChain callbacks (`callbacks.py`); **OTel absent in core** | VERIFIED span taxonomy + pluggable processors (`tracing/create.py`); **not OTel-native** | VERIFIED hard OTel deps + `gen_ai.*` semconv (`telemetry/tracing/gen_ai_shapes.py`) | VERIFIED hard `opentelemetry-api` + `_instrumentation.py` | PARTIAL (`server/monitoring.py` hooks + `llm/metrics.py`; OTel wiring not fully traced) | VERIFIED full OTel stack (`observability.py` L1374/L1425/L1603) |
| Test/eval harness | VERIFIED 71 test files + `checkpoint-conformance/` suite | VERIFIED 415 test files + `MockChatCompletionModel` (`testing/model.py`) | VERIFIED ~315 files + recorded cassettes | VERIFIED 840 files + **`pydantic_evals` first-class package** | VERIFIED `tests/unit\|runtime\|e2e` + `evaluation/` ~30 benchmarks | PARTIAL unit tests only; **no eval harness** (MISSING) |
| Content-safety / moderation gateway | MISSING | MISSING | MISSING | MISSING | MISSING (risk analyzers, not moderation) | MISSING under `ContentSafety\|PromptShield` names (grep-verified) |

## Patterns adopted into this repository

Chosen because they map onto concrete gaps in `xiaohongshu-sentiment-analysis` (a fixed-workflow
CLI that currently has **no** model-driven loop, **no** typed tool registry, **no** execution
trace, and a permission model with no runtime enforcement call site):

1. **Typed next-step ADT for the loop body** (openai-agents `run_steps.py`) — loop pattern-matches
   `CALL_TOOL | FINISH | BLOCKED` instead of scattering `while True` flags.
2. **Three distinct tool-error outcomes** (pydantic-ai `ModelRetry`/`ToolFailedError`, crewAI
   INVALID_INPUT): retryable hint vs hard failure vs invalid input, each budgeted separately.
3. **Per-tool budgets + approval bound to argument hash** (agent-framework `FunctionTool.max_invocations`,
   `_FunctionArgumentsChangedAfterApproval`) — the model cannot swap args after approval.
4. **Append-only event log as source of truth, state as derived sidecar** (OpenHands
   `EventStream` + `State.__getstate__` excluding history) → SQLite `agent_events` table,
   crash-safe resume.
5. **ControlFlag for iteration + budget with raise-on-exhaust** (OpenHands `control_flags.py`)
   — one dataclass reused for step count and spend.
6. **Stuck/loop detector over recent history** (OpenHands `StuckDetector`) — repeated
   action/observation n-grams raise a typed error instead of looping silently.
7. **Approval as loop escape hatch** (agent-framework `_loop.py` breaks on pending approval;
   OpenHands confirmation states) — persist pending request, stop, resume on confirm/deny.
8. **Span taxonomy over pluggable processors, not a hard OTel dependency** (openai-agents
   `tracing/`) — JSONL trace sink first, OTel later.

Deliberately **not** adopted: Docker/K8s runtimes, OTel exporter farms, pickle-based state,
multi-agent workflow engines, vector-store memory. This project is a local CLI with a
single-agent loop; adding those would violate the "business determines agent architecture"
constraint in `docs/agent/architecture.md`.

## Explicit non-findings

- `gh` CLI unavailable (substituted clone + REST; same data source).
- langgraph tool declaration/schema generation lives in `langchain-core` — UNVERIFIED here.
- openai-agents hard token/dollar budget kill-switch: not found in run loop — UNVERIFIED.
- OpenHands `evaluation/` harness contents: directory layout verified, benchmark internals
  not deep-read; maintained successor is `OpenHands/benchmarks` — UNVERIFIED.
- OpenHands OTel exporter wiring location not fully traced — PARTIAL.
- agent-framework `_workflows/` orchestration internals: file inventory verified, bodies not
  fully read — PARTIAL.
- Sandbox internals (`sandbox/*`, OpenHands V1 SDK) — superficial only.
