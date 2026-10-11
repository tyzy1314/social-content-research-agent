#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一平台能力词汇表与访问状态（第四轮建立，第五轮扩展接入模式）。

设计目标：在既有 ``platforms/base.py`` 的 ``BaseAdapter`` 之上声明**能力**与
**访问状态**，而不是新建第二套平台注册系统。所有状态都是显式枚举，
禁止用"返回空列表"伪装平台成功但无内容。

第五轮新增：
* 六统一接入模式（``PUBLIC_READONLY`` / ``OFFICIAL_API`` / ``AUTHORIZED_SESSION``
  / ``USER_IMPORT`` / ``LOCAL_DATABASE`` / ``UNAVAILABLE``）；
* **能力级运行时状态**（``CapabilityStatus``）——状态来自真实探测结果，
  不是硬编码常量；未探测的能力必须显式标记 ``UNKNOWN`` 并说明未探测原因。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


def now_iso() -> str:
    """UTC 时间戳（统一记录"检查时间"用）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

# --------------------------------------------------------------------------- #
# 能力
# --------------------------------------------------------------------------- #
CAP_SEARCH_CONTENT = "search_content"
CAP_CONTENT_DETAIL = "content_detail"
CAP_COMMENTS = "comments"
CAP_REPLIES = "replies"
CAP_BARRAGE = "barrage"
CAP_CREATOR_CONTENT = "creator_content"

ALL_CAPABILITIES: tuple[str, ...] = (
    CAP_SEARCH_CONTENT,
    CAP_CONTENT_DETAIL,
    CAP_COMMENTS,
    CAP_REPLIES,
    CAP_BARRAGE,
    CAP_CREATOR_CONTENT,
)

# --------------------------------------------------------------------------- #
# 单次访问状态
# --------------------------------------------------------------------------- #
ACCESS_OK = "OK"
AUTH_REQUIRED = "AUTH_REQUIRED"          # 需账号授权
PERMISSION_DENIED = "PERMISSION_DENIED"  # 无该接口权限
UNAVAILABLE = "UNAVAILABLE"              # 平台不可达 / 被风控 / 网络失败
RATE_LIMITED = "RATE_LIMITED"            # 被风控限流（如 HTTP 412 / 429）
NOT_SUPPORTED = "NOT_SUPPORTED"          # 该平台不具备此能力
INVALID_INPUT = "INVALID_INPUT"

# --------------------------------------------------------------------------- #
# 平台整体验收等级（第十四节）
# --------------------------------------------------------------------------- #
LIVE_VERIFIED = "LIVE_VERIFIED"        # 真实平台访问 + 关键链路验证通过
DATA_VERIFIED = "DATA_VERIFIED"        # 用合法取得的真实数据完成分析，线上接口未验证
CONTRACT_VERIFIED = "CONTRACT_VERIFIED"  # 适配器与测试通过，真实访问未验证
BLOCKED = "BLOCKED"                    # 受权限/网络/外部条件阻塞
NOT_IMPLEMENTED = "NOT_SUPPORTED"      # 尚未实现

# --------------------------------------------------------------------------- #
# 六统一接入模式（第五轮）
# --------------------------------------------------------------------------- #
MODE_PUBLIC_READONLY = "PUBLIC_READONLY"        # 平台允许的免登录公开只读接口
MODE_OFFICIAL_API = "OFFICIAL_API"              # 官方开放平台 API（需应用凭据）
MODE_AUTHORIZED_SESSION = "AUTHORIZED_SESSION"  # 已授权账号会话 / 浏览器登录态
MODE_USER_IMPORT = "USER_IMPORT"                # 用户主动导入（提供链接/文件）
MODE_LOCAL_DATABASE = "LOCAL_DATABASE"          # 本地已有真实采集数据
MODE_UNAVAILABLE = "UNAVAILABLE"                # 无合法可用路径

ALL_ACCESS_MODES: tuple[str, ...] = (
    MODE_PUBLIC_READONLY,
    MODE_OFFICIAL_API,
    MODE_AUTHORIZED_SESSION,
    MODE_USER_IMPORT,
    MODE_LOCAL_DATABASE,
    MODE_UNAVAILABLE,
)

# 降级链：免登录公开优先（数据最新）；本地真实数据回退；最后才是需要授权的路径。
DEFAULT_MODE_ORDER: tuple[str, ...] = (
    MODE_PUBLIC_READONLY,
    MODE_LOCAL_DATABASE,
    MODE_OFFICIAL_API,
    MODE_AUTHORIZED_SESSION,
    MODE_USER_IMPORT,
    MODE_UNAVAILABLE,
)

# --------------------------------------------------------------------------- #
# 能力级运行时状态（第五轮：由真实探测得出，非硬编码）
# --------------------------------------------------------------------------- #
CAP_AVAILABLE = "AVAILABLE"              # 实测可用
CAP_AUTH_REQUIRED = "AUTH_REQUIRED"      # 需授权/登录态
CAP_RATE_LIMITED = "RATE_LIMITED"        # 被风控限流（如 HTTP 412）
CAP_BLOCKED = "BLOCKED"                  # 被平台拒绝（如 403 / 访客系统）
CAP_NOT_SUPPORTED = "NOT_SUPPORTED"      # 该平台无此能力
CAP_UNKNOWN = "UNKNOWN"                  # 未探测（必须给出未探测原因）

ALL_CAPABILITY_STATUSES: tuple[str, ...] = (
    CAP_AVAILABLE,
    CAP_AUTH_REQUIRED,
    CAP_RATE_LIMITED,
    CAP_BLOCKED,
    CAP_NOT_SUPPORTED,
    CAP_UNKNOWN,
)


@dataclass(frozen=True)
class AccessResult:
    """一次能力检查或数据获取的结果（永不静默返回空数据）。"""

    platform: str
    capability: str
    status: str
    message: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)
    access_mode: str = ""                # 本次实际使用的接入模式
    evidence: Mapping[str, Any] = field(default_factory=dict)  # 原始引用：端点/状态码/样本

    @property
    def ok(self) -> bool:
        return self.status == ACCESS_OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "capability": self.capability,
            "status": self.status,
            "ok": self.ok,
            "message": self.message,
            "access_mode": self.access_mode,
            "evidence": dict(self.evidence),
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class CapabilityStatus:
    """**能力级**运行时状态（第五轮核心）。

    与 ``AccessResult`` 的区别：本对象描述"这项能力现在能不能用、以什么模式用、
    依据是什么"，用于智能体自动降级决策；``AccessResult`` 描述"一次调用的结果"。
    """

    platform: str
    capability: str
    access_mode: str = MODE_UNAVAILABLE
    status: str = CAP_UNKNOWN
    reason: str = ""
    probed: bool = False                 # False = 未发起真实请求，必须说明原因
    checked_at: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.status == CAP_AVAILABLE

    @property
    def needs_auth(self) -> bool:
        return self.status in (CAP_AUTH_REQUIRED, CAP_BLOCKED)

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "capability": self.capability,
            "access_mode": self.access_mode,
            "status": self.status,
            "usable": self.usable,
            "needs_auth": self.needs_auth,
            "reason": self.reason,
            "probed": self.probed,
            "checked_at": self.checked_at,
            "evidence": dict(self.evidence),
        }


def capability_status(
    platform: str,
    capability: str,
    *,
    status: str,
    access_mode: str = MODE_UNAVAILABLE,
    reason: str = "",
    probed: bool = False,
    evidence: Mapping[str, Any] | None = None,
) -> CapabilityStatus:
    """构造能力状态（统一注入检查时间，避免各处自造时间戳格式）。"""
    return CapabilityStatus(
        platform=platform,
        capability=capability,
        access_mode=access_mode,
        status=status,
        reason=reason,
        probed=probed,
        checked_at=now_iso(),
        evidence=dict(evidence or {}),
    )


def not_supported(platform: str, capability: str, why: str = "") -> AccessResult:
    return AccessResult(platform, capability, NOT_SUPPORTED, why or f"{platform} 不支持 {capability}")


@dataclass(frozen=True)
class PlatformInfo:
    """平台在统一注册表中的声明（真实能力 + 真实验收状态 + 接入模式）。"""

    name: str
    display_name: str
    adapter_key: str
    capabilities: tuple[str, ...]
    access_method: str          # 官方API / 公开接口 / 授权接口 / 浏览器 / 本地导入
    verification: str           # LIVE_VERIFIED / BLOCKED / ...
    notes: str = ""
    auth_required: bool = False
    access_modes: tuple[str, ...] = (MODE_UNAVAILABLE,)
    default_access_mode: str = MODE_UNAVAILABLE
    login_free_capabilities: tuple[str, ...] = ()   # 明确可免登录使用的能力

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "adapter_key": self.adapter_key,
            "capabilities": list(self.capabilities),
            "access_method": self.access_method,
            "verification": self.verification,
            "auth_required": self.auth_required,
            "access_modes": list(self.access_modes),
            "default_access_mode": self.default_access_mode,
            "login_free_capabilities": list(self.login_free_capabilities),
            "notes": self.notes,
        }


@dataclass(frozen=True)
class UnifiedContent:
    """跨平台归一化内容记录。"""

    platform: str
    platform_content_id: str
    content_type: str = ""
    title: str = ""
    text: str = ""
    source_url: str = ""
    author_ref: str = ""
    published_at: str = ""
    engagement_metrics: Mapping[str, Any] = field(default_factory=dict)
    raw_metadata: Mapping[str, Any] = field(default_factory=dict)
    source_method: str = ""


@dataclass(frozen=True)
class UnifiedComment:
    """跨平台归一化评论记录（含一级/二级关系）。"""

    platform: str
    platform_comment_id: str
    platform_content_id: str
    content: str
    parent_comment_id: str = ""     # 空 = 一级评论
    author_ref: str = ""
    published_at: str = ""
    engagement_metrics: Mapping[str, Any] = field(default_factory=dict)
    provenance: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_reply(self) -> bool:
        return bool(self.parent_comment_id)
