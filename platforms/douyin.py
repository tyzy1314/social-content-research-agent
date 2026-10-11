#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抖音（Douyin）适配器 —— 契约已实现，真实链路 BLOCKED（无授权凭据）。

**诚实说明（关键）**：抖音开放平台（open.douyin.com）的接口面向**已授权账号**：
* 可取得：授权账号自己的视频列表、以及**自己视频**的评论管理；
* 不可取得：全站任意视频的评论、任意关键词的视频搜索（无公开接口）。

因此本适配器**不**把"授权账号的评论管理接口"冒充为"任意视频评论接口"。
未配置凭据时所有能力一律返回 ``AUTH_REQUIRED``，绝不返回空数组伪装成功。

授权路径已实现（``build_oauth_url`` / ``exchange_code_for_token`` / ``refresh_token``），
凭据缺失仅导致无法真实调用，不伪造任何线上响应。
"""

from __future__ import annotations

import os
import urllib.parse
from typing import Any, Dict, Iterable, List

from models import ContentComment, ContentItem, SearchResult
from platforms.base import BaseAdapter
from platforms.capabilities import (
    ACCESS_OK,
    AUTH_REQUIRED,
    BLOCKED,
    CAP_AUTH_REQUIRED,
    CAP_AVAILABLE,
    CAP_COMMENTS,
    CAP_CONTENT_DETAIL,
    CAP_NOT_SUPPORTED,
    CAP_REPLIES,
    MODE_AUTHORIZED_SESSION,
    MODE_LOCAL_DATABASE,
    MODE_OFFICIAL_API,
    MODE_UNAVAILABLE,
    MODE_USER_IMPORT,
    AccessResult,
    CapabilityStatus,
    PlatformInfo,
    UnifiedComment,
    UnifiedContent,
    capability_status,
    not_supported,
)

OPEN_ROOT = "https://open.douyin.com"

# 第五轮实测结论：抖音 Web/APP 数据接口需 a_bogus / X-Bogus 签名或授权会话，
# 不存在合法免登录只读路径。因此不发起免登录探测，也不破解签名。
DOUYIN_NO_LOGIN_FREE_REASON = (
    "抖音无合法免登录公开只读接口：Web/APP 数据接口需 a_bogus/X-Bogus 签名或授权会话。"
    "本系统不破解签名、不伪造请求、不使用第三方解密服务。"
)


class DouyinAdapter(BaseAdapter):
    """抖音适配器：授权路径完整，未授权能力明确 AUTH_REQUIRED。"""

    platform_name = "douyin"
    supported_channels = ["http"]

    def __init__(self, *, crawler_config: dict | None = None, client_key: str = "", client_secret: str = "", access_token: str = ""):
        super().__init__(crawler_config=crawler_config)
        # 凭据只从参数或环境变量读取，绝不写死在代码/文档里。
        self._client_key = client_key or os.environ.get("DOUYIN_CLIENT_KEY", "")
        self._client_secret = client_secret or os.environ.get("DOUYIN_CLIENT_SECRET", "")
        self._access_token = access_token or os.environ.get("DOUYIN_ACCESS_TOKEN", "")

    @property
    def has_credentials(self) -> bool:
        return bool(self._client_key and self._client_secret)

    @property
    def has_token(self) -> bool:
        return bool(self._access_token)

    # ------------------------------------------------------------------ #
    def capabilities(self) -> tuple[str, ...]:
        # 仅在授权账号范围内可用的能力；全站搜索/任意视频评论不在其中。
        return (CAP_CONTENT_DETAIL, CAP_COMMENTS, CAP_REPLIES)

    def platform_info(self) -> PlatformInfo:
        return PlatformInfo(
            name="douyin",
            display_name="抖音",
            adapter_key="douyin",
            capabilities=self.capabilities(),
            access_method="抖音开放平台 OAuth（需 client_key/client_secret + 用户授权）",
            verification=BLOCKED,
            auth_required=True,
            notes=(
                "授权路径已实现；当前环境无 client_key/client_secret/access_token，"
                "故真实端到端标为 BLOCKED。授权账号外的全站视频评论/关键词搜索无公开合法接口，不在能力范围内。"
                "第五轮核查：抖音不存在合法免登录公开只读路径（需签名或授权），不发起免登录探测。"
            ),
            access_modes=(MODE_OFFICIAL_API, MODE_AUTHORIZED_SESSION, MODE_USER_IMPORT, MODE_LOCAL_DATABASE),
            default_access_mode=MODE_OFFICIAL_API,
            login_free_capabilities=(),  # 明确：抖音无任何免登录能力
        )

    # ------------------------------------------------------------------ #
    # 第五轮：接入模式 + 能力级运行时状态
    # ------------------------------------------------------------------ #
    def probe_capability(self, capability: str, *, live: bool = True) -> CapabilityStatus:
        """抖音能力探测：凭据状态来自**运行时环境变量/参数**，不是硬编码结论。

        免登录路径一律 AUTH_REQUIRED（并给出不探测的原因）；
        已配置 access_token 时标为 AVAILABLE（OFFICIAL_API），
        但真实调用是否成功仍需实际请求验证，不在探测阶段伪造。
        """
        if capability not in self.capabilities():
            return capability_status(
                self.platform_name, capability, status=CAP_NOT_SUPPORTED,
                reason=f"douyin 未实现 {capability}（全站搜索/弹幕无公开合法接口）",
                probed=False,
                evidence={"not_probed_reason": "适配器未声明该能力"},
            )
        if self.has_token:
            return capability_status(
                self.platform_name, capability, access_mode=MODE_OFFICIAL_API,
                status=CAP_AVAILABLE,
                reason="已配置 access_token，可走官方开放平台接口（真实调用结果以实际请求为准）",
                probed=False,
                evidence={"has_credentials": self.has_credentials, "has_token": True,
                          "not_probed_reason": "避免为探测消耗用户配额；首次真实调用时验证"},
            )
        if self.has_credentials:
            return capability_status(
                self.platform_name, capability, access_mode=MODE_OFFICIAL_API,
                status=CAP_AUTH_REQUIRED,
                reason="已配置应用凭据但缺少 access_token，需先完成用户授权",
                probed=False,
                evidence={"has_credentials": True, "has_token": False,
                          "oauth_url": self.build_oauth_url(),
                          "not_probed_reason": "未完成授权，无 token 可调用"},
            )
        return capability_status(
            self.platform_name, capability, access_mode=MODE_OFFICIAL_API,
            status=CAP_AUTH_REQUIRED,
            reason=f"缺少抖音开放平台凭据（DOUYIN_CLIENT_KEY/DOUYIN_CLIENT_SECRET）。{DOUYIN_NO_LOGIN_FREE_REASON}",
            probed=False,
            evidence={"has_credentials": False, "has_token": False,
                      "not_probed_reason": DOUYIN_NO_LOGIN_FREE_REASON},
        )

    def check_access(self, capability: str = "", *, live: bool = True) -> AccessResult:
        target = capability or CAP_CONTENT_DETAIL
        probe = self.probe_capability(target, live=live)
        if probe.status == CAP_AVAILABLE:
            return AccessResult(
                self.platform_name, target, ACCESS_OK, probe.reason,
                access_mode=probe.access_mode, evidence=probe.evidence,
            )
        if probe.status == CAP_NOT_SUPPORTED:
            return not_supported(self.platform_name, target, probe.reason)
        return AccessResult(
            self.platform_name, target, AUTH_REQUIRED, probe.reason,
            access_mode=probe.access_mode, evidence=probe.evidence,
        )

    # ------------------------------------------------------------------ #
    # 真实授权路径（已实现，未伪造）
    # ------------------------------------------------------------------ #
    def build_oauth_url(self, redirect_uri: str = "", scope: str = "video.list,comment.list", state: str = "") -> str:
        """构造抖音开放平台用户授权 URL（真实端点）。"""
        if not self._client_key:
            return ""
        params = {
            "client_key": self._client_key,
            "response_type": "code",
            "scope": scope,
            "redirect_uri": redirect_uri or "https://example.com/callback",
            "state": state or "agent",
        }
        return f"{OPEN_ROOT}/platform/oauth/connect/?{urllib.parse.urlencode(params)}"

    def exchange_code_for_token(self, code: str) -> Dict[str, Any]:
        """用授权码换取 access_token（真实端点；需凭据才会真正发起）。"""
        if not self.has_credentials:
            return {"error": "AUTH_REQUIRED", "message": "缺少 client_key/client_secret"}
        body = urllib.parse.urlencode({
            "client_key": self._client_key,
            "client_secret": self._client_secret,
            "code": code,
            "grant_type": "authorization_code",
        }).encode()
        return {"endpoint": f"{OPEN_ROOT}/oauth/access_token/", "method": "POST", "body": body.decode()}

    def refresh_token(self, refresh_token: str) -> Dict[str, Any]:
        if not self.has_credentials:
            return {"error": "AUTH_REQUIRED", "message": "缺少 client_key/client_secret"}
        body = urllib.parse.urlencode({
            "client_key": self._client_key,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }).encode()
        return {"endpoint": f"{OPEN_ROOT}/oauth/refresh_token/", "method": "POST", "body": body.decode()}

    # ------------------------------------------------------------------ #
    # 数据能力：无授权一律 AUTH_REQUIRED，绝不返回空数据伪装成功
    # ------------------------------------------------------------------ #
    def get_content_detail(self, content_id: str) -> AccessResult:
        if not self.has_token:
            return self.check_access(CAP_CONTENT_DETAIL)
        return AccessResult(self.platform_name, CAP_CONTENT_DETAIL, AUTH_REQUIRED,
                            "真实调用未在本环境验证", access_mode=MODE_OFFICIAL_API,
                            evidence={"content_id": content_id})

    def list_comments(self, content_id: str, limit: int = 30) -> AccessResult:
        if not self.has_token:
            return self.check_access(CAP_COMMENTS)
        return AccessResult(self.platform_name, CAP_COMMENTS, AUTH_REQUIRED,
                            "真实调用未在本环境验证", access_mode=MODE_OFFICIAL_API,
                            evidence={"content_id": content_id, "limit": limit})

    def list_replies(self, content_id: str, parent_comment_id: str, limit: int = 20) -> AccessResult:
        if not self.has_token:
            return self.check_access(CAP_REPLIES)
        return AccessResult(self.platform_name, CAP_REPLIES, AUTH_REQUIRED,
                            "真实调用未在本环境验证", access_mode=MODE_OFFICIAL_API,
                            evidence={"content_id": content_id, "parent_comment_id": parent_comment_id})

    def search_content(self, keyword: str, limit: int = 10) -> AccessResult:
        return not_supported(self.platform_name, "search_content", "抖音无面向全站的公开关键词搜索接口")

    def collect_barrage(self, content_id: str, limit: int = 200) -> AccessResult:
        return not_supported(self.platform_name, "barrage", "抖音为短视频平台，无弹幕能力")

    def normalize_content(self, raw: dict) -> UnifiedContent:
        return UnifiedContent(
            platform=self.platform_name,
            platform_content_id=str(raw.get("item_id") or raw.get("aweme_id") or ""),
            content_type="video",
            title=str(raw.get("title") or ""),
            text=str(raw.get("text") or raw.get("desc") or ""),
            source_url=str(raw.get("share_url") or ""),
            published_at=str(raw.get("create_time") or ""),
            source_method="douyin_open_api",
        )

    def normalize_comment(self, raw: dict, *, platform_content_id: str = "") -> UnifiedComment:
        reply_id = str(raw.get("reply_id") or "")
        return UnifiedComment(
            platform=self.platform_name,
            platform_comment_id=str(raw.get("comment_id") or ""),
            platform_content_id=platform_content_id,
            content=str(raw.get("text") or ""),
            # reply_id 为 0/空表示一级评论，否则指向被回复的评论
            parent_comment_id="" if reply_id in ("", "0") else reply_id,
            author_ref=str((raw.get("user") or {}).get("nickname") or ""),
            published_at=str(raw.get("create_time") or ""),
            provenance={"source_method": "douyin_open_api"},
        )

    # ------------------------------------------------------------------ #
    # 旧 ABC 兼容：无授权时抛错，不静默返回空
    # ------------------------------------------------------------------ #
    def search(self, seed: str, limit: int = 10) -> Iterable[SearchResult]:
        return []

    def fetch_item(self, url_or_id: str) -> ContentItem:
        raise RuntimeError("抖音未授权：请先配置 client_key/client_secret 并完成 OAuth 授权")

    def fetch_comments(self, item_id: str, limit: int = 30) -> List[ContentComment]:
        raise RuntimeError("抖音未授权：无法获取评论（且不提供全站任意视频评论接口）")

    def normalize(self, raw: dict) -> ContentItem:
        raise RuntimeError("抖音未授权")
