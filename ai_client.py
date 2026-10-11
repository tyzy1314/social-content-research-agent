#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 总结接口预留层。
当前默认不调用任何真实 API。
"""

from __future__ import annotations

from typing import Dict, Sequence


class BaseAIClient:
    provider = ""
    model = ""

    def summarize(self, note: Dict[str, object], comments: Sequence[Dict[str, str]]) -> str:
        raise NotImplementedError


class DummyAIClient(BaseAIClient):
    provider = "dummy"
    model = ""

    def summarize(self, note: Dict[str, object], comments: Sequence[Dict[str, str]]) -> str:
        return ""


class OpenAICompatibleClient(BaseAIClient):
    def __init__(self, model: str = "", api_key_env: str = "OPENAI_API_KEY", base_url: str = ""):
        self.provider = "openai_compatible"
        self.model = model or ""
        self.api_key_env = api_key_env
        self.base_url = base_url

    def summarize(self, note: Dict[str, object], comments: Sequence[Dict[str, str]]) -> str:
        return ""


def build_ai_client(ai_config: Dict[str, object] | None = None) -> BaseAIClient:
    config = ai_config or {}
    if not config.get("enabled", False):
        return DummyAIClient()

    provider = str(config.get("provider") or "").strip().lower()
    if provider == "openai_compatible":
        return OpenAICompatibleClient(
            model=str(config.get("model") or ""),
            api_key_env=str(config.get("api_key_env") or "OPENAI_API_KEY"),
            base_url=str(config.get("base_url") or ""),
        )

    return DummyAIClient()
