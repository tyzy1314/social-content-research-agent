#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""尚未取得授权的平台适配器（快手/微博/知乎/贴吧）。

原则：**不伪装**。这些平台当前均无法合法取得数据（缺官方开放授权或接口已收紧），
因此：
* ``capabilities()`` 如实声明（多数为空或仅契约级）；
* ``check_access()`` 一律返回 ``AUTH_REQUIRED`` / ``NOT_SUPPORTED``；
* 数据方法**从不返回空数组**表示成功，而是返回明确的阻塞状态；
* 验收等级标记为 ``BLOCKED``。

绝不使用空方法 + "已支持"描述冒充完成。

第五轮补充：对**存在历史公开端点**的平台（微博、贴吧），提供真实运行时探测
（单次请求、遇限制即停）；对**无任何合法免登录端点**的平台（知乎、快手），
明确标记"未发起请求"并写明原因，不伪造状态。
"""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Tuple

from models import ContentComment, ContentItem, SearchResult
from platforms.base import BaseAdapter
from platforms.capabilities import (
    AUTH_REQUIRED,
    BLOCKED,
    CAP_AUTH_REQUIRED,
    CAP_BLOCKED,
    CAP_NOT_SUPPORTED,
    MODE_AUTHORIZED_SESSION,
    MODE_LOCAL_DATABASE,
    MODE_OFFICIAL_API,
    MODE_PUBLIC_READONLY,
    MODE_UNAVAILABLE,
    MODE_USER_IMPORT,
    NOT_IMPLEMENTED,
    AccessResult,
    CapabilityStatus,
    PlatformInfo,
    UnifiedComment,
    UnifiedContent,
    capability_status,
    not_supported,
)

PROBE_TIMEOUT = 12
PROBE_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def _http_get(url: str) -> Tuple[int, str]:
    """单次 GET，绝不重试、绝不换 UA/IP。返回 (status, body)。"""
    request = urllib.request.Request(url, headers={"User-Agent": PROBE_UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as response:
            return response.status, response.read(20000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read(2000).decode("utf-8", "replace")
        except Exception:
            return exc.code, ""
    except Exception as exc:
        return -1, f"{type(exc).__name__}: {exc}"


class PendingAdapter(BaseAdapter):
    """未授权平台的公共基类：所有能力默认阻塞。"""

    display_name = ""
    access_method = ""
    notes = ""
    declared_capabilities: Tuple[str, ...] = ()
    # 注意：这里用 declared_* 前缀，避免遮蔽 BaseAdapter 的同名方法
    declared_access_modes: Tuple[str, ...] = (MODE_LOCAL_DATABASE, MODE_USER_IMPORT)
    declared_default_access_mode: str = MODE_UNAVAILABLE
    # 子类可声明：{capability: url}，用于**真实**免登录可达性探测（各 1 次请求）
    probe_endpoints: Dict[str, str] = {}
    # 子类可声明：命中即判定为"被平台拒绝/需登录"的页面特征
    blocked_markers: Tuple[str, ...] = ()
    # 子类可声明：命中即判定为"接口真的返回了可用数据"的特征
    success_markers: Tuple[str, ...] = ()
    # 无合法探测端点时的说明（必须写明"未发起请求"的原因）
    no_probe_reason: str = ""

    def capabilities(self) -> Tuple[str, ...]:
        return self.declared_capabilities

    def platform_info(self) -> PlatformInfo:
        return PlatformInfo(
            name=self.platform_name,
            display_name=self.display_name,
            adapter_key=self.platform_name,
            capabilities=self.capabilities(),
            access_method=self.access_method,
            verification=BLOCKED if self.declared_capabilities else NOT_IMPLEMENTED,
            auth_required=bool(self.declared_capabilities),
            notes=self.notes,
            access_modes=self.declared_access_modes,
            default_access_mode=self.declared_default_access_mode,
            login_free_capabilities=(),
        )

    def access_modes(self) -> Tuple[str, ...]:
        return self.declared_access_modes

    def default_access_mode(self) -> str:
        return self.declared_default_access_mode

    # ------------------------------------------------------------------ #
    # 第五轮：运行时探测（有端点则真实请求一次；无端点则如实标记未探测）
    # ------------------------------------------------------------------ #
    def probe_capability(self, capability: str, *, live: bool = True) -> CapabilityStatus:
        url = self.probe_endpoints.get(capability, "")
        if not url:
            return capability_status(
                self.platform_name, capability, access_mode=MODE_UNAVAILABLE,
                status=CAP_AUTH_REQUIRED if capability in self.declared_capabilities else CAP_NOT_SUPPORTED,
                reason=self.no_probe_reason or f"{self.display_name} 无合法免登录端点，未发起请求",
                probed=False,
                evidence={"not_probed_reason": self.no_probe_reason or "无合法免登录公开端点"},
            )
        if not live:
            return capability_status(
                self.platform_name, capability, access_mode=MODE_UNAVAILABLE,
                status="UNKNOWN", reason="未发起真实请求（live=False）", probed=False,
                evidence={"not_probed_reason": "离线模式", "endpoint": url},
            )
        status, body = _http_get(url)
        evidence: Dict[str, Any] = {"endpoint": url, "http_status": status, "body_head": body[:160]}
        if status == -1:
            return capability_status(self.platform_name, capability, status="UNAVAILABLE",
                                     reason=f"探测失败：{body[:120]}", probed=True, evidence=evidence)
        if status in (401, 403):
            return capability_status(self.platform_name, capability, status=CAP_BLOCKED,
                                     reason=f"{self.display_name} 拒绝访客访问 HTTP {status}",
                                     probed=True, evidence=evidence)
        if status in (412, 429):
            return capability_status(self.platform_name, capability, status="RATE_LIMITED",
                                     reason=f"{self.display_name} 触发风控 HTTP {status}（已停止请求，不绕过）",
                                     probed=True, evidence=evidence)
        if any(marker in body for marker in self.blocked_markers):
            return capability_status(self.platform_name, capability, status=CAP_BLOCKED,
                                     reason=f"返回页含登录/访客系统特征：{self.blocked_markers}（需登录态，不绕过）",
                                     probed=True, evidence=evidence)
        if status == 200 and any(marker in body for marker in self.success_markers):
            return capability_status(self.platform_name, capability, access_mode=MODE_PUBLIC_READONLY,
                                     status="AVAILABLE",
                                     reason="公开端点返回了可用数据（免登录）", probed=True, evidence=evidence)
        return capability_status(self.platform_name, capability, status=CAP_BLOCKED,
                                 reason=(f"公开端点 HTTP {status} 但未返回可用数据"
                                         f"（期望特征 {self.success_markers} 未命中）"),
                                 probed=True, evidence=evidence)

    def check_access(self, capability: str = "", *, live: bool = True) -> AccessResult:
        target = capability or "content_detail"
        if self.declared_capabilities and target in self.declared_capabilities:
            probe = self.probe_capability(target, live=live)
            return AccessResult(
                self.platform_name, target, AUTH_REQUIRED,
                f"{self.display_name} 需官方授权，当前无凭据；运行时探测：{probe.status} - {probe.reason}",
                access_mode=probe.access_mode, evidence=probe.evidence,
            )
        return not_supported(self.platform_name, target, f"{self.display_name} 当前不具备 {target} 能力")

    def search_content(self, keyword: str, limit: int = 10) -> AccessResult:
        return self.check_access("search_content")

    def get_content_detail(self, content_id: str) -> AccessResult:
        return self.check_access("content_detail")

    def list_comments(self, content_id: str, limit: int = 30) -> AccessResult:
        return self.check_access("comments")

    def normalize_content(self, raw: dict) -> UnifiedContent:
        return UnifiedContent(
            platform=self.platform_name,
            platform_content_id=str(raw.get("id") or ""),
            title=str(raw.get("title") or ""),
            text=str(raw.get("text") or ""),
            source_method=f"{self.platform_name}_pending",
        )

    def normalize_comment(self, raw: dict, *, platform_content_id: str = "") -> UnifiedComment:
        return UnifiedComment(
            platform=self.platform_name,
            platform_comment_id=str(raw.get("id") or ""),
            platform_content_id=platform_content_id,
            content=str(raw.get("text") or ""),
            provenance={"source_method": f"{self.platform_name}_pending"},
        )

    def search(self, seed: str, limit: int = 10) -> Iterable[SearchResult]:
        raise RuntimeError(f"{self.display_name} 未授权，无法搜索")

    def fetch_item(self, url_or_id: str) -> ContentItem:
        raise RuntimeError(f"{self.display_name} 未授权，无法获取内容")

    def fetch_comments(self, item_id: str, limit: int = 30) -> List[ContentComment]:
        raise RuntimeError(f"{self.display_name} 未授权，无法获取评论")

    def normalize(self, raw: dict) -> ContentItem:
        raise RuntimeError(f"{self.display_name} 未授权")


class KuaishouAdapter(PendingAdapter):
    platform_name = "kuaishou"
    display_name = "快手"
    access_method = "快手开放平台（需企业资质 + OAuth 授权）"
    declared_capabilities = ("content_detail", "comments")
    notes = "快手开放平台主要面向授权账号/企业应用，当前无凭据；未提供全站公开搜索接口。"
    declared_access_modes = (MODE_OFFICIAL_API, MODE_AUTHORIZED_SESSION, MODE_LOCAL_DATABASE, MODE_USER_IMPORT)
    declared_default_access_mode = MODE_OFFICIAL_API
    no_probe_reason = (
        "快手无面向公众的免登录只读数据接口（Web/APP 接口需登录态与签名），"
        "本系统不破解签名、不伪造会话，故未发起免登录探测请求。"
    )


class WeiboAdapter(PendingAdapter):
    platform_name = "weibo"
    display_name = "微博"
    access_method = "微博开放平台（需 App Key/Secret + 用户授权，且需应用审核）"
    declared_capabilities = ("search_content", "comments")
    notes = (
        "微博开放平台需创建应用并通过审核；未授权前无法合法调用，不做未授权抓取。"
        "第五轮实测：m.weibo.cn 公开容器接口返回『Sina Visitor System』访客页，"
        "weibo.com/ajax/side/hotSearch 返回 HTTP 403，均无可用免登录公开内容。"
    )
    declared_access_modes = (MODE_OFFICIAL_API, MODE_AUTHORIZED_SESSION, MODE_LOCAL_DATABASE, MODE_USER_IMPORT)
    declared_default_access_mode = MODE_OFFICIAL_API
    # 真实探测：单次请求，遇访客系统特征即判定 BLOCKED，绝不重试/换 IP
    probe_endpoints = {
        "search_content": "https://m.weibo.cn/api/container/getIndex?containerid=100103type%3D1%26q%3D%E5%8D%97%E4%BA%AC%E7%BE%8E%E9%A3%9F",
    }
    blocked_markers = ("Sina Visitor System", "passport", "请登录")
    success_markers = ('"ok":1', '"cards"')


class ZhihuAdapter(PendingAdapter):
    platform_name = "zhihu"
    display_name = "知乎"
    access_method = "无官方开放数据接口（需登录态，存在风控）"
    declared_capabilities = ()
    notes = "知乎未提供面向研究的官方开放 API；登录态抓取涉及风控与合规风险，故标记 NOT_SUPPORTED，不实现抓取。"
    declared_access_modes = (MODE_LOCAL_DATABASE, MODE_USER_IMPORT)
    declared_default_access_mode = MODE_UNAVAILABLE
    no_probe_reason = (
        "知乎无合法免登录公开只读接口（内容需登录态，且存在风控）；"
        "本系统不绕过登录态与风控，故未发起免登录探测请求。"
    )


class TiebaAdapter(PendingAdapter):
    platform_name = "tieba"
    display_name = "百度贴吧"
    access_method = "无官方开放数据接口"
    declared_capabilities = ()
    notes = (
        "百度贴吧无稳定官方开放接口，历史上接口已收紧；标记 NOT_SUPPORTED，不实现抓取。"
        "第五轮实测：hottopic/browse/hotall 返回 HTTP 200 但仅约 2.5KB 占位页，无话题/帖子数据。"
    )
    declared_access_modes = (MODE_LOCAL_DATABASE, MODE_USER_IMPORT)
    declared_default_access_mode = MODE_UNAVAILABLE
    probe_endpoints = {
        "search_content": "https://tieba.baidu.com/hottopic/browse/hotall",
    }
    blocked_markers = ("Sina Visitor System",)
    success_markers = ("topic_name", "topic_id")
