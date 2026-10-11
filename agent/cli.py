#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI surface for ``python main.py agent`` (frozen contract: ``docs/agent/architecture.md``).

Builds the runtime from ``config.yaml`` and command-line flags, wires the
business tool registry, drives a run (or resumes one), and renders the result as
either a machine-readable ``RunResult`` JSON or a human-readable step trace.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

from agent.permissions import PermissionGate
from agent.providers import OpenAICompatibleProvider, OpencodeCliProvider, ScriptedProvider
from agent.runtime import AgentRuntime, RuntimeConfig, new_run_id
from agent.state import AgentStore
from agent.tracing import MultiTraceSink, SqliteTraceSink

# Heavy business wiring (pulls in db/crawl_planner/discovery/export_utils) is imported
# lazily so that merely importing this module — or ``main.py`` for its parser — does not
# force the crawler stack to load.
from agent_tools import build_default_registry, set_db_path


def add_agent_parser(subparsers: Any) -> argparse.ArgumentParser:
    """Register the ``agent`` subcommand on an existing argparse subparsers collection."""
    parser = subparsers.add_parser("agent", help="运行模型驱动的智能体闭环")
    parser.add_argument("task", help="自然语言任务描述")
    parser.add_argument(
        "--provider",
        choices=["opencode", "openai", "scripted"],
        default="opencode",
        help="模型提供者：opencode（本地 CLI）/ openai（兼容端点）/ scripted（仅测试，需 --script）",
    )
    parser.add_argument("--script", default=None, help="scripted 提供者的脚本 JSON 文件路径（仅 --provider scripted）")
    parser.add_argument("--max-steps", type=int, default=10, help="单轮运行最大步数（默认 10）")
    parser.add_argument("--run-id", default=None, help="指定 run_id（恢复时必填）")
    parser.add_argument("--resume", action="store_true", help="恢复 --run-id 指定的未完成任务")
    parser.add_argument("--yes", action="store_true", help="无头模式：批准所有副作用工具 (approval_policy=allow_all)")
    parser.add_argument("--db", default=None, help="爬虫 SQLite 数据库路径（工具操作的库；默认读 config.yaml）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出 RunResult")
    # Real-model connection (Priority 1). Key is taken from --api-key or the
    # AGENT_API_KEY env var — never persisted to config.yaml or printed in logs.
    parser.add_argument("--api-key", default=None, help="OpenAI 兼容端点 API key（也可用环境变量 AGENT_API_KEY；不写入配置）")
    parser.add_argument("--base-url", default=None, help="OpenAI 兼容端点 base URL（默认读 config.yaml ai.base_url）")
    parser.add_argument("--model", default=None, help="模型标识（默认读 config.yaml ai.model，回退 glm-5.1）")
    parser.add_argument("--timeout", type=float, default=300.0, help="单次模型请求超时秒数（默认 300）")
    parser.add_argument("--max-seconds", type=float, default=None, help="总任务墙钟超时秒数（默认不限）")
    parser.add_argument("--max-tokens", type=int, default=None, help="总 token 预算（默认不限）")
    parser.add_argument("--cancel", action="store_true", help="取消 --run-id 指定的运行中任务")
    return parser


def _build_provider(name: str, *, config: Mapping[str, Any], script_path: str | None, sink: Any, args: Any) -> Any:
    """Construct a model provider from the resolved name, config, and trace sink."""
    if name == "scripted":
        if not script_path:
            raise SystemExit("scripted provider 需要 --script <path.json>")
        path = Path(script_path)
        if not path.exists():
            raise SystemExit(f"script 文件不存在: {script_path}")
        script = json.loads(path.read_text(encoding="utf-8"))
        return ScriptedProvider(script, sink=sink)

    if name == "openai":
        ai = config.get("ai") or {}
        base_url = args.base_url or str(ai.get("base_url") or "https://api.openai.com/v1")
        api_key = (
            args.api_key
            or str(ai.get("api_key") or "")
            or os.environ.get("AGENT_API_KEY", "")
        )
        model = args.model or str(ai.get("model") or "glm-5.1")
        if not api_key:
            raise SystemExit(
                "openai provider 缺少 API key：请用 --api-key、config.yaml ai.api_key，"
                "或环境变量 AGENT_API_KEY 提供（密钥不写入日志/文档）"
            )
        return OpenAICompatibleProvider(
            base_url=base_url, api_key=api_key, model=model,
            timeout=float(getattr(args, "timeout", 300.0)), sink=sink,
        )

    if name == "opencode":
        ai = config.get("ai") or {}
        model = args.model or str(ai.get("model") or "opencode/mimo-v2.6-flash-free")
        return OpencodeCliProvider(model=model, sink=sink)

    raise SystemExit(f"未知 provider: {name}")


def run_agent(args: argparse.Namespace, config: Mapping[str, Any]) -> int:
    """Entry point invoked by ``main.py`` for the ``agent`` subcommand.

    Returns a process exit code: ``0`` for a finished run, ``2`` otherwise.
    """
    if getattr(args, "db", None):
        set_db_path(args.db)

    approval_policy = "allow_all" if args.yes else "deny_mutations"
    config_obj = RuntimeConfig(
        max_steps=args.max_steps,
        approval_policy=approval_policy,
        max_wall_clock_seconds=args.max_seconds,
        max_total_tokens=args.max_tokens,
    )

    store = AgentStore()
    if getattr(args, "cancel", False):
        if not args.run_id:
            raise SystemExit("--cancel 需要 --run-id")
        store.update_run(args.run_id, status="cancelled")
        print(f"已请求取消任务 {args.run_id}（运行中的循环会在下一个步边界停止）")
        return 0
    registry = build_default_registry(store)
    gate = PermissionGate(allow_all=args.yes)

    run_id = args.run_id or new_run_id()
    sink = MultiTraceSink([SqliteTraceSink(store, run_id=run_id)])

    try:
        provider = _build_provider(
            args.provider, config=config, script_path=args.script, sink=sink, args=args
        )
        runtime = AgentRuntime(
            provider=provider, registry=registry, store=store, gate=gate,
            config=config_obj, sink=sink,
        )

        if args.resume:
            if not args.run_id:
                raise SystemExit("--resume 需要 --run-id")
            result = runtime.resume(args.run_id)
        else:
            result = runtime.run(args.task, run_id=args.run_id)
    finally:
        sink.close()

    if args.json:
        print(json.dumps(_runresult_to_dict(result), ensure_ascii=False, indent=2, default=str))
    else:
        _print_human(result)

    return 0 if result.status == "finished" else 2


def _runresult_to_dict(result: Any) -> dict[str, Any]:
    """Serialise a :class:`RunResult` (events included) to plain dicts."""
    return {
        "run_id": result.run_id,
        "status": result.status,
        "final_text": result.final_text,
        "steps_used": result.steps_used,
        "tool_calls": result.tool_calls,
        "usage": dict(result.usage),
        "error": result.error,
        "events": [
            {
                "seq": e.seq,
                "ts": e.ts,
                "step": e.step,
                "role": e.role,
                "kind": e.kind,
                "payload": dict(e.payload),
            }
            for e in result.events
        ],
    }


def _print_human(result: Any) -> None:
    """Render a concise, human-readable step trace to stdout."""
    print(f"# Agent run {result.run_id}  ->  {result.status}")
    if result.error:
        print(f"! error: {result.error}")
    for event in result.events:
        if event.role == "trace":
            continue
        if event.kind == "task":
            print(f"  [task] {event.payload.get('task', '')}")
        elif event.kind == "decision":
            print(f"  step {event.step} -> call {event.payload.get('tool')}({_short(event.payload.get('args', {}))})")
        elif event.kind == "tool_result":
            ok = event.payload.get("ok")
            body = event.payload.get("content") or event.payload.get("error") or ""
            flag = "ok" if ok else "ERR"
            print(f"       <- {flag}: {_short(body, 200)}")
        elif event.kind == "final":
            print(f"  step {event.step} -> final: {event.payload.get('final_text', '')}")
        elif event.kind == "error":
            print(f"  step {event.step} [error] {event.payload}")
    print(f"# steps_used={result.steps_used} tool_calls={result.tool_calls} usage={result.usage}")
    if result.final_text:
        print(f"\n## Final answer\n{result.final_text}")


def _short(value: Any, limit: int = 120) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


if __name__ == "__main__":
    # Allow `python -m agent.cli` for quick local checks (expects a parsed args-like).
    raise SystemExit("use `python main.py agent ...`")
