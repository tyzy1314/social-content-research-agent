#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微博平台占位适配器。"""

from __future__ import annotations

from platforms.base import BaseAdapter


class WeiboAdapter(BaseAdapter):
    platform_name = "weibo"
    supported_channels = ["api", "http", "browser", "ocr", "import"]

    def search(self, seed: str, limit: int = 10):
        raise NotImplementedError("weibo adapter is a placeholder")

    def fetch_item(self, url_or_id: str):
        raise NotImplementedError("weibo adapter is a placeholder")

    def fetch_comments(self, item_id: str, limit: int = 30):
        raise NotImplementedError("weibo adapter is a placeholder")

    def normalize(self, raw: dict):
        raise NotImplementedError("weibo adapter is a placeholder")
