"""Model providers for the agent runtime: scripted mock, OpenAI-compatible HTTP, opencode CLI.

Every provider normalises its transport into a :class:`~agent.types.ModelTurn` (providers
never return raw SDK objects) and emits a ``provider_call`` trace event through an
optional ``sink=`` constructor argument (extension beyond the frozen contract, documented
in ``docs/agent/architecture.md``'s TraceSink section).
"""
# allow: SIZE — contract layout mandates one providers module for the ABC + 3 implementations; split into per-class subtasks with mocks instead of live calls.

from __future__ import annotations

import json
import random
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Mapping, Sequence

from agent.errors import ModelError, ModelProtocolError, ProviderNotAvailable
from agent.tracing import TraceSink
from agent.types import Event, ModelTurn, ToolCall

# langgraph RetryPolicy numbers from the architecture contract.
_RETRY_INITIAL_INTERVAL = 0.5
_RETRY_FACTOR = 2.0
_RETRY_MAX_INTERVAL = 128.0


def _utc_now() -> str:
    """Return the current UTC time as a microsecond-precision ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _ms_since(started: float) -> int:
    """Return whole milliseconds elapsed since a :func:`time.monotonic` timestamp."""
    return int((time.monotonic() - started) * 1000)


def _backoff_delay(attempt: int) -> float:
    """Return the sleep before retry ``attempt`` (0-based): exp backoff plus jitter.

    Uses the contract's langgraph numbers: initial 0.5s, factor 2.0, capped at 128s,
    with +/-50% jitter so concurrent callers do not retry in lockstep.
    """
    base = min(_RETRY_INITIAL_INTERVAL * (_RETRY_FACTOR ** attempt), _RETRY_MAX_INTERVAL)
    return min(base * random.uniform(0.5, 1.5), _RETRY_MAX_INTERVAL)


def _emit_trace(sink: TraceSink | None, *, provider: str, step: int, payload: Mapping[str, Any]) -> None:
    """Emit one ``provider_call`` span; a missing sink disables tracing (no-op).

    The event's ``seq`` is a placeholder — ``SqliteTraceSink`` assigns the authoritative
    value via ``AgentStore.append_event``.
    """
    if sink is None:
        return
    merged: dict[str, Any] = {"provider": provider}
    merged.update(payload)
    sink.emit(Event(seq=0, ts=_utc_now(), step=step, role="trace", kind="provider_call", payload=merged))


def _coerce_turn(entry: ModelTurn | Mapping[str, Any], index: int) -> ModelTurn:
    """Convert one ScriptedProvider script entry into a :class:`ModelTurn`.

    Dict form is ``{"tool": str, "args": dict}`` or ``{"final": str}``. Malformed
    entries are a test-authoring bug, so they fail fast with :class:`ValueError`.
    """
    if isinstance(entry, ModelTurn):
        return entry
    if isinstance(entry, Mapping):
        has_tool = "tool" in entry
        has_final = "final" in entry
        if has_tool and has_final:
            raise ValueError(f"script entry {index} must define either 'tool' or 'final', not both")
        if has_tool:
            tool = entry["tool"]
            if not isinstance(tool, str) or not tool:
                raise ValueError(f"script entry {index}: 'tool' must be a non-empty string")
            args = entry.get("args", {})
            if not isinstance(args, Mapping):
                raise ValueError(f"script entry {index}: 'args' must be a mapping")
            return ModelTurn(
                tool_calls=(ToolCall(tool=tool, args=args, call_id=f"call_{index}"),),
                finish_reason="tool_calls",
            )
        if has_final:
            final = entry["final"]
            if not isinstance(final, str):
                raise ValueError(f"script entry {index}: 'final' must be a string")
            return ModelTurn(final_text=final, finish_reason="stop")
        raise ValueError(f"script entry {index} must contain 'tool' or 'final'")
    raise ValueError(f"script entry {index} must be a ModelTurn or mapping")


def _latest_user_task(messages: Sequence[Mapping[str, Any]]) -> str:
    """Return the content of the last user message (the agent loop's task)."""
    task = ""
    for message in messages:
        if message.get("role") == "user":
            content = message.get("content")
            task = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return task


def _strip_fences(text: str) -> str:
    """Strip a markdown code fence (```...```) from ``text`` when one wraps the payload."""
    match = re.search(r"```[a-zA-Z]*\s*(.*?)```", text, re.DOTALL)
    return match.group(1).strip() if match else text.strip()


def _find_decision(text: str) -> dict[str, Any]:
    """Return the first JSON object in ``text`` that carries ``action`` or ``tool``.

    Scans for balanced JSON objects with :meth:`json.JSONDecoder.raw_decode`, so
    surrounding prose and nested argument objects are handled.

    Raises:
        ModelProtocolError: No decision object was found.
    """
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            candidate, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(candidate, dict) and ("action" in candidate or "tool" in candidate):
            return candidate
    raise ModelProtocolError("no JSON decision object found in provider output")


def _decision_final_text(decision: Mapping[str, Any], args: Mapping[str, Any], raw_text: str) -> str:
    """Extract the final answer from a FINISH decision.

    Looks for ``final`` on the decision itself, then ``final_text``/``final``/``text``/
    ``answer`` in its args; falls back to the provider's full reply text so no
    information is silently dropped.
    """
    for source in (
        decision.get("final"),
        args.get("final_text"),
        args.get("final"),
        args.get("text"),
        args.get("answer"),
    ):
        if isinstance(source, str):
            return source
    return raw_text


def _tool_names(tools: Sequence[Mapping[str, Any]]) -> set[str]:
    """Extract tool names from an OpenAI-style ``tools`` array (or bare name dicts)."""
    names: set[str] = set()
    for spec in tools:
        if not isinstance(spec, Mapping):
            continue
        function = spec.get("function")
        name = function.get("name") if isinstance(function, Mapping) else spec.get("name")
        if isinstance(name, str):
            names.add(name)
    return names


class ModelProvider(ABC):
    """Base class for model providers.

    Message format is OpenAI-style dicts: ``{"role": ..., "content": ...}`` plus
    ``{"role": "tool", "tool_call_id": ..., "content": ...}`` for results; providers
    translate as needed.
    """

    name: ClassVar[str]

    @abstractmethod
    def complete(
        self,
        *,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        step: int,
    ) -> ModelTurn:
        """Run one model turn and return the normalised result."""

    def close(self) -> None:
        """Release provider resources. Default: no-op."""


class ScriptedProvider(ModelProvider):
    """Replay a fixed script of turns. **Mock only — not evidence of real model behaviour.**

    Tier-1 test double: never touches the network or a real model; it exists so loop,
    budget, stuck, and message-rebuilding logic can be exercised deterministically.
    A pass with this provider says nothing about how a real model behaves.

    Args:
        script: Sequence of :class:`~agent.types.ModelTurn` objects or dict-form entries
            ``{"tool": str, "args": dict}`` / ``{"final": str}``.
        sink: Optional trace sink; receives a ``provider_call`` event per ``complete``.
    """

    name: ClassVar[str] = "scripted"

    def __init__(
        self,
        script: Sequence[ModelTurn | Mapping[str, Any]],
        *,
        sink: TraceSink | None = None,
    ) -> None:
        self._turns = tuple(_coerce_turn(entry, index) for index, entry in enumerate(script))
        self._index = 0
        self._sink = sink

    def complete(
        self,
        *,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        step: int,
    ) -> ModelTurn:
        """Return the next scripted turn.

        Raises:
            ModelProtocolError: The script is exhausted.
        """
        started = time.monotonic()
        try:
            if self._index >= len(self._turns):
                raise ModelProtocolError(f"scripted provider exhausted after {len(self._turns)} turns")
            turn = self._turns[self._index]
            self._index += 1
        except Exception as exc:
            _emit_trace(
                self._sink,
                provider=self.name,
                step=step,
                payload={
                    "duration_ms": _ms_since(started),
                    "error": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            raise
        _emit_trace(
            self._sink,
            provider=self.name,
            step=step,
            payload={
                "duration_ms": _ms_since(started),
                "turn_index": self._index - 1,
                "finish_reason": turn.finish_reason,
                "tool_calls": len(turn.tool_calls),
            },
        )
        return turn


class OpenAICompatibleProvider(ModelProvider):
    """Call any OpenAI-compatible ``/chat/completions`` endpoint over stdlib urllib.

    Translates native ``tool_calls[]`` into :class:`~agent.types.ToolCall` tuples and
    ``usage`` into the standard ``prompt``/``completion``/``total`` keys. Retries
    transport errors, HTTP 429, and 5xx with exponential backoff plus jitter; any
    non-429 4xx fails immediately without retry.

    Args:
        base_url: API root; ``/chat/completions`` is appended.
        api_key: Sent as ``Authorization: Bearer <api_key>``.
        model: Model identifier for the request body.
        timeout: Per-request socket timeout in seconds.
        max_retries: Retries after the first attempt (so at most ``max_retries + 1``
            requests are sent).
        sink: Optional trace sink; receives a ``provider_call`` event per ``complete``.
    """

    name: ClassVar[str] = "openai"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 120.0,
        max_retries: int = 3,
        sink: TraceSink | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._max_retries = max_retries
        self._sink = sink

    def complete(
        self,
        *,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        step: int,
    ) -> ModelTurn:
        """POST one chat-completions request (with retries) and parse the turn.

        Raises:
            ModelError: Non-429 4xx, or retries exhausted on 429/5xx/transport errors.
            ModelProtocolError: The 200 response body is not parseable chat-completions
                JSON (malformed JSON, missing choices, non-JSON tool arguments).
        """
        started = time.monotonic()
        body = {
            "model": self._model,
            "messages": [{"role": "system", "content": system}, *messages],
            "tools": list(tools),
            "tool_choice": "auto",
        }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        attempts = 0
        try:
            for attempt in range(self._max_retries + 1):
                attempts = attempt + 1
                if attempt:
                    time.sleep(_backoff_delay(attempt - 1))
                try:
                    raw = self._post(data)
                except urllib.error.HTTPError as exc:
                    status = exc.code
                    detail = exc.read().decode("utf-8", "replace")
                    exc.close()
                    if status == 429 or 500 <= status <= 599:
                        if attempt >= self._max_retries:
                            raise ModelError(
                                f"model endpoint returned HTTP {status} after {attempts} attempts"
                            ) from exc
                        continue
                    raise ModelError(f"model endpoint returned HTTP {status}: {detail[:300]}") from exc
                except OSError as exc:
                    if attempt >= self._max_retries:
                        raise ModelError(
                            f"transport error calling model endpoint: {type(exc).__name__}: {exc}"
                        ) from exc
                    continue
                turn = self._parse_response(raw, step=step)
                break
            else:
                raise ModelError("model endpoint retries exhausted")
        except Exception as exc:
            _emit_trace(
                self._sink,
                provider=self.name,
                step=step,
                payload={
                    "model": self._model,
                    "duration_ms": _ms_since(started),
                    "attempts": attempts,
                    "error": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            raise
        _emit_trace(
            self._sink,
            provider=self.name,
            step=step,
            payload={
                "model": self._model,
                "duration_ms": _ms_since(started),
                "attempts": attempts,
                "finish_reason": turn.finish_reason,
                "tool_calls": len(turn.tool_calls),
                "usage": dict(turn.usage),
            },
        )
        return turn

    def _post(self, data: bytes) -> bytes:
        """POST ``data`` to the chat-completions endpoint and return the raw body."""
        request = urllib.request.Request(
            self._url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            return response.read()

    def _parse_response(self, raw: bytes, *, step: int) -> ModelTurn:
        """Translate a chat-completions body into a :class:`ModelTurn`."""
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ModelProtocolError("model endpoint returned a malformed JSON body") from exc
        try:
            choice = body["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelProtocolError("model response is missing choices[0].message") from exc
        if not isinstance(message, Mapping):
            raise ModelProtocolError("model response message is not an object")
        tool_calls: list[ToolCall] = []
        raw_calls = message.get("tool_calls") or []
        for call in raw_calls:
            if not isinstance(call, Mapping) or not isinstance(call.get("function"), Mapping):
                raise ModelProtocolError("malformed tool_calls entry in model response")
            function = call["function"]
            tool_name = function.get("name")
            if not isinstance(tool_name, str) or not tool_name:
                raise ModelProtocolError("tool call is missing function.name")
            arguments = function.get("arguments") or "{}"
            try:
                args = json.loads(arguments)
            except (json.JSONDecodeError, TypeError) as exc:
                raise ModelProtocolError(f"tool call for {tool_name!r} has non-JSON arguments") from exc
            if not isinstance(args, dict):
                raise ModelProtocolError(f"tool call for {tool_name!r} arguments must be a JSON object")
            call_id = call.get("id") or f"call_{step}"
            tool_calls.append(ToolCall(tool=tool_name, args=args, call_id=str(call_id)))
        content = message.get("content")
        final_text = content if (content is not None and not tool_calls) else None
        usage: dict[str, int] = {}
        raw_usage = body.get("usage") if isinstance(body, Mapping) else None
        if isinstance(raw_usage, Mapping):
            key_map = (("prompt_tokens", "prompt"), ("completion_tokens", "completion"), ("total_tokens", "total"))
            for source, target in key_map:
                value = raw_usage.get(source)
                if isinstance(value, int):
                    usage[target] = value
        finish_reason = choice.get("finish_reason") if isinstance(choice, Mapping) else None
        return ModelTurn(
            tool_calls=tuple(tool_calls),
            final_text=final_text,
            finish_reason=finish_reason,
            usage=usage,
            raw=message,
        )


class OpencodeCliProvider(ModelProvider):
    """Drive the local ``opencode run`` CLI as a JSON-decision model provider (Tier 3).

    Hard constraints from the architecture contract, all verified empirically on this
    machine: the CLI runs in an isolated throwaway working directory (its tools must
    never see the project tree — a default agent in the project root was observed
    calling ``glob``/``bash`` and querying the real DB); tools cannot be disabled via
    ``opencode.json`` (free tier answers 403), so isolation plus sanitisation are the
    mitigation; any ``tool_use`` event in the subprocess output is a protocol violation
    that is rejected after ``max_retries`` stricter re-prompts; and the subprocess is
    killed on a hard wall-clock timeout.

    Args:
        model: Model identifier passed as ``--model``.
        workdir: Isolated working directory; a fresh temp dir is created when omitted.
        timeout: Hard wall-clock limit per subprocess run, in seconds.
        max_retries: Contamination re-prompts allowed after the initial attempt.
        sink: Optional trace sink; receives a ``provider_call`` event per ``complete``.
    """

    name: ClassVar[str] = "opencode"

    def __init__(
        self,
        *,
        model: str = "opencode/mimo-v2.6-flash-free",
        workdir: str | Path | None = None,
        timeout: float = 120.0,
        max_retries: int = 2,
        sink: TraceSink | None = None,
    ) -> None:
        self._model = model
        self._timeout = timeout
        self._max_retries = max_retries
        self._sink = sink
        if workdir is None:
            self._workdir = Path(tempfile.mkdtemp(prefix="agent-opencode-"))
            self._owns_workdir = True
        else:
            self._workdir = Path(workdir)
            self._workdir.mkdir(parents=True, exist_ok=True)
            self._owns_workdir = False
        config = self._workdir / "opencode.json"
        if not config.exists():
            config.write_text(
                json.dumps({"$schema": "https://opencode.ai/config.json"}, ensure_ascii=False),
                encoding="utf-8",
            )

    def complete(
        self,
        *,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        step: int,
    ) -> ModelTurn:
        """Run the CLI once (or several times on contamination) and parse its decision.

        Raises:
            ProviderNotAvailable: The ``opencode`` binary is not on PATH.
            ModelError: The subprocess timed out or exited non-zero.
            ModelProtocolError: Output was non-JSON, contained a ``tool_use`` event
                (contamination), or held no valid ``{"action", "args"}`` decision.
        """
        started = time.monotonic()
        task = _latest_user_task(messages)
        attempts = 0
        try:
            for attempt in range(self._max_retries + 1):
                attempts = attempt + 1
                prompt = self._build_prompt(system, task, tools, attempt=attempt)
                try:
                    stdout = self._run_cli(prompt)
                except FileNotFoundError as exc:
                    raise ProviderNotAvailable("opencode CLI not found on PATH") from exc
                events = self._parse_events(stdout)
                if any(event.get("type") == "tool_use" for event in events):
                    continue
                turn = self._decide(events, tools=tools, step=step)
                break
            else:
                raise ModelProtocolError("provider emitted tool_use events")
        except Exception as exc:
            _emit_trace(
                self._sink,
                provider=self.name,
                step=step,
                payload={
                    "model": self._model,
                    "duration_ms": _ms_since(started),
                    "attempts": attempts,
                    "error": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            raise
        _emit_trace(
            self._sink,
            provider=self.name,
            step=step,
            payload={
                "model": self._model,
                "duration_ms": _ms_since(started),
                "attempts": attempts,
                "finish_reason": turn.finish_reason,
                "tool_calls": len(turn.tool_calls),
            },
        )
        return turn

    def close(self) -> None:
        """Remove the throwaway workdir when this provider created it."""
        if self._owns_workdir:
            shutil.rmtree(self._workdir, ignore_errors=True)

    def _build_prompt(
        self,
        system: str,
        task: str,
        tools: Sequence[Mapping[str, Any]],
        *,
        attempt: int,
    ) -> str:
        """Compose the single-turn decision prompt for the CLI subprocess.

        Four genuinely independent inputs are required: the system instructions, the
        task text, the tool specs (name, description, JSON schema), and the retry
        attempt (0 = initial, 1+ = stricter re-prompt after contamination).
        """
        sections: list[str] = []
        if attempt:
            sections.append(
                "Your previous reply attempted to use tools, which is forbidden here. "
                "Reply again: a tool-using reply will be rejected."
            )
        if system:
            sections.append(f"System instructions:\n{system}")
        sections.append(f"Task:\n{task}")
        tool_lines: list[str] = []
        for spec in tools:
            if not isinstance(spec, Mapping):
                continue
            function = spec.get("function") if isinstance(spec.get("function"), Mapping) else spec
            name = function.get("name", "?")
            description = function.get("description", "")
            tool_lines.append(f"- {name}: {description}")
            parameters = function.get("parameters")
            if parameters:
                tool_lines.append(f"  parameters: {json.dumps(parameters, ensure_ascii=False)}")
        if tool_lines:
            sections.append("Available tools (call at most one via \"action\"):\n" + "\n".join(tool_lines))
        sections.append(
            "Respond with ONLY a JSON object. Do not use any tools.\n"
            'The JSON object must have the shape {"action": "<tool name>|FINISH", "args": {...}}.'
        )
        return "\n\n".join(sections)

    def _run_cli(self, prompt: str) -> str:
        """Run ``opencode run --pure --format json`` in the isolated workdir; return stdout.

        Raises:
            FileNotFoundError: The ``opencode`` binary is absent (mapped to
                :class:`ProviderNotAvailable` by :meth:`complete`).
            ModelError: Wall-clock timeout (process killed) or non-zero exit.
        """
        command = ["opencode", "run", "--pure", "--format", "json", "--model", self._model, prompt]
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            command,
            cwd=str(self._workdir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            stdout, stderr = process.communicate(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise ModelError(f"opencode CLI timed out after {self._timeout:g}s") from None
        if process.returncode != 0:
            tail = (stderr or "").strip()[-500:]
            suffix = f": {tail}" if tail else ""
            raise ModelError(f"opencode CLI exited with code {process.returncode}{suffix}")
        return stdout

    def _parse_events(self, stdout: str) -> list[dict[str, Any]]:
        """Parse CLI stdout as JSONL: one JSON object per non-blank line."""
        events: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ModelProtocolError(f"opencode CLI emitted a non-JSON line: {stripped[:200]}") from exc
            if not isinstance(event, dict):
                raise ModelProtocolError("opencode CLI emitted a JSON value that is not an object")
            events.append(event)
        return events

    def _decide(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]],
        step: int,
    ) -> ModelTurn:
        """Translate ``text`` events into a FINISH or single-tool :class:`ModelTurn`."""
        parts: list[str] = []
        for event in events:
            if event.get("type") != "text":
                continue
            part = event.get("part")
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(event.get("text"), str):
                parts.append(event["text"])
        if not parts:
            raise ModelProtocolError("opencode CLI produced no text decision")
        text = _strip_fences("".join(parts))
        decision = _find_decision(text)
        action = decision.get("action") or decision.get("tool")
        if not isinstance(action, str) or not action:
            raise ModelProtocolError("decision JSON is missing 'action'")
        args = decision.get("args", {})
        if not isinstance(args, dict):
            raise ModelProtocolError("decision JSON 'args' must be an object")
        if action.upper() == "FINISH":
            return ModelTurn(final_text=_decision_final_text(decision, args, text), finish_reason="stop")
        names = _tool_names(tools)
        if action not in names:
            raise ModelProtocolError(f"decision references unknown action {action!r}")
        return ModelTurn(
            tool_calls=(ToolCall(tool=action, args=args, call_id=f"call_{step}"),),
            finish_reason="tool_calls",
        )
