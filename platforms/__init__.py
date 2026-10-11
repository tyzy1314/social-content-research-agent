#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""平台适配器注册入口（第四轮：统一注册表）。

在**原有** ``ADAPTERS`` 之上扩展，不新建第二套注册系统：
* ``bilibili`` 现指向公开 HTTP 接口适配器（已真实验证，无需浏览器）；
* 旧的浏览器版适配器保留为 ``bilibili_browser``，避免破坏既有流程；
* 每个平台通过 ``platform_info()`` 声明真实能力与真实验收等级。

延迟导入：避免 `python main.py --help` 加载 Selenium/浏览器依赖。
"""

from __future__ import annotations

from importlib import import_module
from typing import Any, Dict, List

ADAPTERS: Dict[str, str] = {
    "xiaohongshu": "platforms.xiaohongshu:XhsAdapter",
    "bilibili": "platforms.bilibili_api:BilibiliApiAdapter",
    "bilibili_browser": "platforms.bilibili:BilibiliAdapter",  # 旧浏览器版，保留兼容
    "douyin": "platforms.douyin:DouyinAdapter",
    "kuaishou": "platforms.pending:KuaishouAdapter",
    "weibo": "platforms.pending:WeiboAdapter",
    "zhihu": "platforms.pending:ZhihuAdapter",
    "tieba": "platforms.pending:TiebaAdapter",
}

#: 面向用户的展示顺序（第一/第二/第三优先级）
PLATFORM_ORDER: List[str] = [
    "xiaohongshu", "bilibili", "douyin", "kuaishou", "weibo", "zhihu", "tieba",
]


def get_adapter_class(platform: str):
    target = ADAPTERS[platform]
    module_name, class_name = target.split(":", 1)
    module = import_module(module_name)
    return getattr(module, class_name)


def get_adapter(platform: str, **kwargs: Any):
    """实例化一个平台适配器。"""
    return get_adapter_class(platform)(**kwargs)


def list_platforms() -> List[Dict[str, Any]]:
    """返回所有平台的声明（能力、接入方式、真实验收等级）。

    个别平台导入失败时**如实标注**为不可用，而不是跳过或伪装成正常。
    """
    infos: List[Dict[str, Any]] = []
    for name in PLATFORM_ORDER:
        if name not in ADAPTERS:
            continue
        try:
            adapter = get_adapter(name)
            infos.append(adapter.platform_info().as_dict())
        except Exception as exc:  # 导入/实例化失败必须可见
            infos.append({
                "name": name,
                "display_name": name,
                "adapter_key": name,
                "capabilities": [],
                "access_method": "unknown",
                "verification": "BLOCKED",
                "auth_required": True,
                "notes": f"适配器不可用: {type(exc).__name__}: {exc}",
            })
    return infos


def get_platform_info(platform: str) -> Dict[str, Any]:
    return get_adapter(platform).platform_info().as_dict()


__all__ = [
    "ADAPTERS",
    "PLATFORM_ORDER",
    "get_adapter_class",
    "get_adapter",
    "list_platforms",
    "get_platform_info",
]
