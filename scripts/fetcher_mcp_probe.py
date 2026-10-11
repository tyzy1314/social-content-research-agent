#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-off Fetcher MCP probe for public web pages.

This is an optional manual diagnostic entrypoint. It starts ``npx -y
fetcher-mcp`` on a temporary localhost MCP HTTP endpoint, calls ``fetch_url``
once, writes the returned content to ``outputs/mcp/``, and exits. It is
intentionally not part of pytest or engineering_review.ps1 because it may
download/run an npm package, launch a Playwright browser, and access an external
website.
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PACKAGE = "fetcher-mcp"
PROTOCOL_VERSION = "2024-11-05"


class McpHttpClient:
    def __init__(self, command: list[str], *, base_url: str, startup_timeout_seconds: int = 60):
        self.command = command
        self.base_url = base_url.rstrip("/")
        self.startup_timeout_seconds = startup_timeout_seconds
        self.process: subprocess.Popen[str] | None = None
        self._next_id = 1
        self.session_id = ""

    def __enter__(self) -> "McpHttpClient":
        self.process = subprocess.Popen(
            self.command,
            cwd=PROJECT_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self._wait_for_server()
        self.initialize()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if not self.process:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def initialize(self) -> None:
        response = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "xhs-project-fetcher-mcp-probe", "version": "0.1.0"},
            },
            timeout_seconds=self.startup_timeout_seconds,
        )
        if "result" not in response:
            raise RuntimeError(f"MCP initialize failed: {response}")
        self.notify("notifications/initialized", {})

    def request(self, method: str, params: dict[str, Any] | None = None, *, timeout_seconds: int = 60) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        response = self._post_json(payload, timeout_seconds=timeout_seconds)
        if response.get("id") != request_id:
            raise RuntimeError(f"Unexpected MCP response id: {response}")
        if "error" in response:
            raise RuntimeError(f"MCP request failed: {response['error']}")
        return response

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._post_json(payload, timeout_seconds=10, allow_empty=True)

    def _wait_for_server(self) -> None:
        deadline = time.monotonic() + self.startup_timeout_seconds
        while time.monotonic() < deadline:
            if self.process and self.process.poll() is not None:
                raise RuntimeError(f"MCP HTTP server exited with code {self.process.returncode}: {self._stderr_tail()}")
            try:
                response = requests.get(self.base_url, timeout=1)
                if response.status_code < 500:
                    return
            except requests.RequestException:
                time.sleep(0.3)
        raise TimeoutError(f"Timed out waiting for MCP HTTP server at {self.base_url}. stderr:\n{self._stderr_tail()}")

    def _post_json(self, payload: dict[str, Any], *, timeout_seconds: int, allow_empty: bool = False) -> dict[str, Any]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        response = requests.post(f"{self.base_url}/mcp", json=payload, headers=headers, timeout=timeout_seconds)
        if response.headers.get("mcp-session-id"):
            self.session_id = str(response.headers["mcp-session-id"])
        if allow_empty and response.status_code in {200, 202, 204} and not response.text.strip():
            return {}
        response.raise_for_status()
        return self._parse_mcp_http_response(response)

    def _parse_mcp_http_response(self, response: requests.Response) -> dict[str, Any]:
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" not in content_type:
            return response.json()

        parsed_events: list[dict[str, Any]] = []
        data_lines: list[str] = []

        def flush_event() -> None:
            if not data_lines:
                return
            candidates = ["\n".join(data_lines), "".join(data_lines)]
            for candidate in candidates:
                try:
                    parsed_events.append(json.loads(candidate))
                    break
                except json.JSONDecodeError:
                    continue
            data_lines.clear()

        response_text = response.content.decode("utf-8", errors="replace")
        for line in response_text.splitlines():
            if not line.strip():
                flush_event()
                continue
            if line.startswith("data:"):
                data_lines.append(line.removeprefix("data:").strip())
        flush_event()
        if not parsed_events:
            raise ValueError(f"Could not parse MCP SSE response: {response_text[:1000]}")
        return parsed_events[-1]

    def _stderr_tail(self) -> str:
        if not self.process or not self.process.stderr:
            return ""
        if self.process.poll() is None:
            return ""
        try:
            _, stderr = self.process.communicate(timeout=1)
            return stderr or ""
        except Exception:
            return ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch one public URL through fetcher-mcp and save the result.")
    parser.add_argument("--url", required=True, help="Public URL to fetch once through Fetcher MCP.")
    parser.add_argument("--timeout-ms", type=int, default=30000, help="Page load timeout passed to fetch_url.")
    parser.add_argument(
        "--wait-until",
        default="load",
        choices=["load", "domcontentloaded", "networkidle", "commit"],
        help="Playwright navigation completion mode passed to fetch_url.",
    )
    parser.add_argument("--max-length", type=int, default=20000, help="Maximum returned content length.")
    parser.add_argument("--return-html", action="store_true", help="Return HTML instead of Markdown.")
    parser.add_argument("--debug", action="store_true", help="Ask fetcher-mcp to show a browser window for manual diagnosis.")
    parser.add_argument("--package", default=DEFAULT_PACKAGE, help="npm package name to run with npx.")
    parser.add_argument("--output-dir", default="outputs/mcp", help="Directory for fetched content and metadata.")
    return parser.parse_args()


def validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Only explicit http/https public URLs are supported.")


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def content_text(result: dict[str, Any]) -> str:
    content = result.get("result", {}).get("content", [])
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
    return "\n".join(part for part in parts if part)


def write_outputs(*, output_dir: Path, url: str, arguments: dict[str, Any], response: dict[str, Any], text: str, suffix: str) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    content_path = output_dir / f"fetcher_mcp_{timestamp}.{suffix}"
    meta_path = output_dir / f"fetcher_mcp_{timestamp}.json"
    content_path.write_text(text, encoding="utf-8")
    meta = {
        "url": url,
        "arguments": arguments,
        "content_path": str(content_path),
        "is_error": response.get("result", {}).get("isError", False),
        "content_items": len(response.get("result", {}).get("content", [])),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return content_path, meta_path


def main() -> int:
    args = parse_args()
    validate_public_url(args.url)

    npx_path = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx_path:
        raise FileNotFoundError("npx was not found. Install Node.js/npm first, then re-run this script.")
    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    command = [
        npx_path,
        "-y",
        args.package,
        "--transport=http",
        "--host=127.0.0.1",
        f"--port={port}",
    ]
    fetch_arguments: dict[str, Any] = {
        "url": args.url,
        "timeout": max(1000, int(args.timeout_ms)),
        "waitUntil": args.wait_until,
        "extractContent": not args.return_html,
        "returnHtml": bool(args.return_html),
        "maxLength": max(1, int(args.max_length)),
        "disableMedia": True,
        "debug": bool(args.debug),
    }

    print("Fetcher MCP probe")
    print(f"command: {' '.join(command)}")
    print(f"mcp_endpoint: {base_url}/mcp")
    print(f"url: {args.url}")
    print("scope: single fetch_url call, no batch, no retry")

    with McpHttpClient(command, base_url=base_url) as client:
        response = client.request(
            "tools/call",
            {"name": "fetch_url", "arguments": fetch_arguments},
            timeout_seconds=max(10, int(args.timeout_ms / 1000) + 30),
        )

    text = content_text(response)
    suffix = "html" if args.return_html else "md"
    content_path, meta_path = write_outputs(
        output_dir=PROJECT_ROOT / args.output_dir,
        url=args.url,
        arguments=fetch_arguments,
        response=response,
        text=text,
        suffix=suffix,
    )

    print(f"is_error: {response.get('result', {}).get('isError', False)}")
    print(f"content_length: {len(text)}")
    print(f"content_path: {content_path}")
    print(f"metadata_path: {meta_path}")
    return 0 if text else 2


if __name__ == "__main__":
    raise SystemExit(main())
