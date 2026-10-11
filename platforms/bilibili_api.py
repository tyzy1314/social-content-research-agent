#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bilibili 适配器（公开 HTTP 接口，无需登录）。

接入方式优先级 3：**平台允许的公开数据访问方式**。
使用 B站 Web 端公开接口（与站点自身相同的只读端点），只取公开可见数据，
不绕过验证码、登录墙或风控；被风控时如实返回 ``UNAVAILABLE`` 而非空数据。

已实测能力（本仓库环境）：
* 视频详情 ``/x/web-interface/view``  -> 标题/简介/UP主/统计(data.stat)
* 评论     ``/x/v2/reply``            -> 一级评论
* 回复     ``/x/v2/reply/reply``      -> 二级评论
* 弹幕     ``comment.bilibili.com/<cid>.xml``（raw deflate 压缩）
* 搜索     ``/x/web-interface/search/type``
"""

from __future__ import annotations

import gzip
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from models import ContentComment, ContentItem, EngagementMetrics, SearchResult
from platforms.base import BaseAdapter
from platforms.capabilities import (
    ACCESS_OK,
    AUTH_REQUIRED,
    BLOCKED,
    CAP_AUTH_REQUIRED,
    CAP_AVAILABLE,
    CAP_BARRAGE,
    CAP_BLOCKED,
    CAP_COMMENTS,
    CAP_CONTENT_DETAIL,
    CAP_CREATOR_CONTENT,
    CAP_NOT_SUPPORTED,
    CAP_RATE_LIMITED,
    CAP_REPLIES,
    CAP_SEARCH_CONTENT,
    LIVE_VERIFIED,
    MODE_LOCAL_DATABASE,
    MODE_PUBLIC_READONLY,
    MODE_USER_IMPORT,
    RATE_LIMITED,
    UNAVAILABLE,
    AccessResult,
    CapabilityStatus,
    PlatformInfo,
    UnifiedComment,
    UnifiedContent,
    capability_status,
    not_supported,
)

API_ROOT = "https://api.bilibili.com"
DANMAKU_ROOT = "https://comment.bilibili.com"
BVID_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# 探测样本：库内已有真实采集记录的视频（避免为探测而额外拉取陌生内容）
PROBE_BVID = "BV1YtAoz3Er3"
PROBE_AID = "116164688612344"
PROBE_CID = "36422222832"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ts_to_iso(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return ""


class BilibiliApiAdapter(BaseAdapter):
    """Bilibili 公开接口适配器（真实网络访问，非 mock）。"""

    platform_name = "bilibili"
    supported_channels = ["http"]

    def __init__(self, *, crawler_config: dict | None = None, timeout: float = 20.0, min_interval: float = 1.0):
        super().__init__(crawler_config=crawler_config)
        self._timeout = timeout
        self._min_interval = min_interval
        self._last_request_at = 0.0
        self._resolved: Dict[str, Dict[str, Any]] = {}
        self._probe_cache: Dict[str, CapabilityStatus] = {}

    # ------------------------------------------------------------------ #
    # 传输层
    # ------------------------------------------------------------------ #
    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _request(self, url: str, *, accept: str = "application/json") -> Tuple[int, bytes]:
        self._throttle()
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Referer": "https://www.bilibili.com/",
                "Accept": accept,
                "Accept-Encoding": "gzip, deflate",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:  # 4xx/5xx
            return exc.code, exc.read() if hasattr(exc, "read") else b""

    @staticmethod
    def _decode(raw: bytes) -> bytes:
        """Decode a body that may be gzip / raw-deflate / plain."""
        if raw[:2] == b"\x1f\x8b":
            try:
                return gzip.decompress(raw)
            except OSError:
                return raw
        try:
            # Bilibili danmaku XML is served as *raw* deflate (no zlib header).
            return zlib.decompress(raw, -15)
        except zlib.error:
            return raw

    def _get_json(self, url: str) -> Dict[str, Any]:
        status, raw = self._request(url)
        if status != 200:
            raise RuntimeError(f"HTTP {status} from {url}")
        text = self._decode(raw).decode("utf-8", "replace")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"non-JSON body from {url}") from exc
        code = payload.get("code")
        if code != 0:
            raise RuntimeError(f"bilibili api code={code} message={payload.get('message')}")
        return payload.get("data") or {}

    # ------------------------------------------------------------------ #
    # ID 解析
    # ------------------------------------------------------------------ #
    def _resolve(self, content_id: str) -> Dict[str, Any]:
        """Resolve bvid/aid/URL -> {"bvid","aid","cid"}."""
        if content_id in self._resolved:
            return self._resolved[content_id]
        bvid = ""
        aid = ""
        match = BVID_RE.search(content_id or "")
        if match:
            bvid = match.group(1)
        elif (content_id or "").strip().isdigit():
            aid = (content_id or "").strip()
        if not bvid and not aid:
            raise ValueError(f"无法从 {content_id!r} 解析 B站视频 ID（需要 BV号、aid 或视频链接）")
        query = f"bvid={bvid}" if bvid else f"aid={aid}"
        data = self._get_json(f"{API_ROOT}/x/web-interface/view?{query}")
        resolved = {
            "bvid": data.get("bvid") or bvid,
            "aid": str(data.get("aid") or aid),
            "cid": str(data.get("cid") or ""),
            "title": data.get("title") or "",
        }
        self._resolved[str(resolved["bvid"])] = resolved
        self._resolved[str(resolved["aid"])] = resolved
        self._resolved[content_id] = resolved
        return resolved

    # ------------------------------------------------------------------ #
    # 能力声明
    # ------------------------------------------------------------------ #
    def capabilities(self) -> tuple[str, ...]:
        return (CAP_SEARCH_CONTENT, CAP_CONTENT_DETAIL, CAP_COMMENTS, CAP_REPLIES, CAP_BARRAGE)

    def platform_info(self) -> PlatformInfo:
        return PlatformInfo(
            name="bilibili",
            display_name="哔哩哔哩",
            adapter_key="bilibili",
            capabilities=self.capabilities(),
            access_method="平台公开 Web 接口（只读，无需登录）",
            verification=LIVE_VERIFIED,
            notes=(
                "免登录公开可用：视频详情/评论/二级回复/弹幕；"
                "关键词搜索接口存在风控（实测返回 HTTP 412），被限流时如实标记 RATE_LIMITED 且不绕过。"
                "UP主作品列表需 wbi 签名/登录态，不实现。"
            ),
            access_modes=(MODE_PUBLIC_READONLY, MODE_LOCAL_DATABASE, MODE_USER_IMPORT),
            default_access_mode=MODE_PUBLIC_READONLY,
            # 仅声明实测可免登录的能力；搜索不在其中（受风控影响，运行时另行判定）
            login_free_capabilities=(CAP_CONTENT_DETAIL, CAP_COMMENTS, CAP_REPLIES, CAP_BARRAGE),
        )

    # ------------------------------------------------------------------ #
    # 第五轮：能力级运行时探测（真实请求，单次，遇限制即停）
    # ------------------------------------------------------------------ #
    def _probe_json(self, url: str) -> Tuple[int, Any, str]:
        """返回 (http_status, api_code, message)；不抛异常，供探测判定。"""
        status, raw = self._request(url)
        if status != 200:
            return status, None, f"HTTP {status}"
        try:
            payload = json.loads(self._decode(raw).decode("utf-8", "replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return status, None, "non-JSON body"
        return status, payload.get("code"), str(payload.get("message") or "")

    def probe_capability(self, capability: str, *, live: bool = True) -> CapabilityStatus:
        """对单项能力做**真实**运行时探测（每个能力一次请求，不重试）。

        限制信号处理（立即停止并记录，绝不换 UA / 换 IP / 伪造请求 / 破解签名）：
        * HTTP 412 / 429                     -> RATE_LIMITED
        * HTTP 401 / 403                     -> BLOCKED（平台拒绝访客访问）
        * api code -101 / -111 / -403        -> AUTH_REQUIRED（需登录态）
        * 网络异常 / 5xx                      -> UNAVAILABLE
        """
        if capability not in self.capabilities():
            return capability_status(
                self.platform_name, capability, status=CAP_NOT_SUPPORTED,
                reason=f"bilibili 未实现 {capability}（{'UP主作品列表需 wbi 签名/登录态' if capability == CAP_CREATOR_CONTENT else '适配器未声明'}）",
                probed=False,
                evidence={"not_probed_reason": "适配器未声明该能力，无需发起请求"},
            )
        if not live:
            cached = self._probe_cache.get(capability)
            if cached is not None:
                return cached
            return capability_status(
                self.platform_name, capability, status="UNKNOWN",
                reason="未发起真实请求（live=False）", probed=False,
                evidence={"not_probed_reason": "离线模式，避免无谓网络请求"},
            )
        if capability in self._probe_cache:
            return self._probe_cache[capability]

        result = self._do_probe(capability)
        self._probe_cache[capability] = result
        return result

    def _do_probe(self, capability: str) -> CapabilityStatus:
        evidence: Dict[str, Any] = {}
        try:
            if capability == CAP_CONTENT_DETAIL:
                url = f"{API_ROOT}/x/web-interface/view?bvid={PROBE_BVID}"
                status, code, message = self._probe_json(url)
                evidence = {"endpoint": url, "http_status": status, "api_code": code, "sample_bvid": PROBE_BVID}
                if status == 412 or status == 429:
                    return capability_status(self.platform_name, capability, status=CAP_RATE_LIMITED,
                                            reason=f"视频详情接口触发风控 HTTP {status}，本次停止请求（不换 IP/UA、不重试）",
                                            probed=True, evidence=evidence)
                if status in (401, 403):
                    return capability_status(self.platform_name, capability, status=CAP_BLOCKED,
                                            reason=f"视频详情接口拒绝访客访问 HTTP {status}",
                                            probed=True, evidence=evidence)
                if status != 200 or code != 0:
                    return capability_status(self.platform_name, capability, status="UNAVAILABLE",
                                            reason=f"视频详情接口不可用：HTTP {status} / code={code} {message}",
                                            probed=True, evidence=evidence)
                return capability_status(self.platform_name, capability, status=CAP_AVAILABLE,
                                        access_mode=MODE_PUBLIC_READONLY,
                                        reason="视频详情接口免登录可达（code=0）", probed=True, evidence=evidence)

            if capability == CAP_COMMENTS:
                url = f"{API_ROOT}/x/v2/reply?type=1&oid={PROBE_AID}&pn=1&ps=20&sort=2"
                # 单次请求：同时取 HTTP 状态、api code 与"声明条数 vs 实际返回条数"
                status, raw = self._request(url)
                code: Any = None
                message = ""
                declared = returned = 0
                if status == 200:
                    try:
                        parsed = json.loads(self._decode(raw).decode("utf-8", "replace"))
                        code = parsed.get("code")
                        message = str(parsed.get("message") or "")
                        data = parsed.get("data") or {}
                        declared = int((data.get("page") or {}).get("count") or 0)
                        returned = len(data.get("replies") or [])
                    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
                        code, message = None, "non-JSON body"
                partial = declared > 0 and returned < min(declared, 20)
                evidence = {"endpoint": url, "http_status": status, "api_code": code, "sample_aid": PROBE_AID,
                            "declared_total": declared, "returned": returned, "partial": partial,
                            "note": "ps 需 >= 20，较小值会返回空 replies（接口行为，非风控）；"
                                    "平台声明条目数大于实际返回时记为 partial（平台侧限流，不绕过）"}
                if status == 412 or status == 429:
                    return capability_status(self.platform_name, capability, status=CAP_RATE_LIMITED,
                                            reason=f"评论接口触发风控 HTTP {status}，本次停止请求",
                                            probed=True, evidence=evidence)
                if status in (401, 403):
                    return capability_status(self.platform_name, capability, status=CAP_BLOCKED,
                                            reason=f"评论接口拒绝访客访问 HTTP {status}", probed=True, evidence=evidence)
                if status != 200 or code != 0:
                    return capability_status(self.platform_name, capability, status="UNAVAILABLE",
                                            reason=f"评论接口不可用：HTTP {status} / code={code} {message}",
                                            probed=True, evidence=evidence)
                reason = "评论接口免登录可达（code=0，返回真实评论）"
                if partial:
                    reason += f"；注意：平台声明 {declared} 条但本次仅返回 {returned} 条（平台侧限流，未绕过）"
                return capability_status(self.platform_name, capability, status=CAP_AVAILABLE,
                                        access_mode=MODE_PUBLIC_READONLY,
                                        reason=reason, probed=True, evidence=evidence)

            if capability == CAP_REPLIES:
                # 二级回复探测：先取一条真实一级评论的 rpid 作为 root（2 次请求，非重试）
                base = f"{API_ROOT}/x/v2/reply?type=1&oid={PROBE_AID}&pn=1&ps=20&sort=2"
                status, code, message = self._probe_json(base)
                if status != 200 or code != 0:
                    return capability_status(self.platform_name, capability, status="UNAVAILABLE",
                                            reason=f"二级回复探测失败：无法取得一级评论（HTTP {status} / code={code}）",
                                            probed=True,
                                            evidence={"endpoint": base, "http_status": status, "api_code": code})
                root = ""
                try:
                    replies = json.loads(self._decode(self._request(base)[1]).decode("utf-8", "replace"))["data"]["replies"] or []
                    if replies:
                        root = str(replies[0].get("rpid") or "")
                except Exception:
                    root = ""
                url = f"{API_ROOT}/x/v2/reply/reply?type=1&oid={PROBE_AID}&root={root}&pn=1&ps=10"
                status, code, message = self._probe_json(url)
                evidence = {"endpoint": url, "http_status": status, "api_code": code, "sample_root": root}
                if status == 412 or status == 429:
                    return capability_status(self.platform_name, capability, status=CAP_RATE_LIMITED,
                                            reason=f"二级回复接口触发风控 HTTP {status}", probed=True, evidence=evidence)
                if status != 200 or code != 0:
                    return capability_status(self.platform_name, capability, status="UNAVAILABLE",
                                            reason=f"二级回复接口不可用：HTTP {status} / code={code} {message}",
                                            probed=True, evidence=evidence)
                return capability_status(self.platform_name, capability, status=CAP_AVAILABLE,
                                        access_mode=MODE_PUBLIC_READONLY,
                                        reason="二级回复接口免登录可达（code=0）", probed=True, evidence=evidence)

            if capability == CAP_BARRAGE:
                url = f"{DANMAKU_ROOT}/{PROBE_CID}.xml"
                status, raw = self._request(url, accept="application/xml")
                ok = status == 200 and b"<d p=" in self._decode(raw)
                evidence = {"endpoint": url, "http_status": status, "sample_cid": PROBE_CID}
                if status == 412 or status == 429:
                    return capability_status(self.platform_name, capability, status=CAP_RATE_LIMITED,
                                            reason=f"弹幕接口触发风控 HTTP {status}", probed=True, evidence=evidence)
                if status in (401, 403):
                    return capability_status(self.platform_name, capability, status=CAP_BLOCKED,
                                            reason=f"弹幕接口拒绝访客访问 HTTP {status}", probed=True, evidence=evidence)
                if not ok:
                    return capability_status(self.platform_name, capability, status="UNAVAILABLE",
                                            reason=f"弹幕接口未返回有效 XML（HTTP {status}）", probed=True, evidence=evidence)
                return capability_status(self.platform_name, capability, status=CAP_AVAILABLE,
                                        access_mode=MODE_PUBLIC_READONLY,
                                        reason="弹幕 XML 免登录可达", probed=True, evidence=evidence)

            if capability == CAP_SEARCH_CONTENT:
                url = f"{API_ROOT}/x/web-interface/search/type?search_type=video&keyword={urllib.parse.quote('南京美食')}&page=1"
                status, code, message = self._probe_json(url)
                evidence = {"endpoint": url, "http_status": status, "api_code": code}
                if status == 412 or status == 429:
                    return capability_status(self.platform_name, capability, status=CAP_RATE_LIMITED,
                                            reason=(f"搜索接口触发风控 HTTP {status}（B站搜索风控），"
                                                    "本次立即停止请求，不换 IP / 不换 UA / 不伪造参数；"
                                                    "可改用明确视频链接（BV号）采集"),
                                            probed=True, evidence=evidence)
                if status in (401, 403):
                    return capability_status(self.platform_name, capability, status=CAP_BLOCKED,
                                            reason=f"搜索接口拒绝访客访问 HTTP {status}", probed=True, evidence=evidence)
                if status != 200 or code != 0:
                    return capability_status(self.platform_name, capability, status="UNAVAILABLE",
                                            reason=f"搜索接口不可用：HTTP {status} / code={code} {message}",
                                            probed=True, evidence=evidence)
                return capability_status(self.platform_name, capability, status=CAP_AVAILABLE,
                                        access_mode=MODE_PUBLIC_READONLY,
                                        reason="搜索接口当前可达（code=0）", probed=True, evidence=evidence)

        except Exception as exc:
            return capability_status(self.platform_name, capability, status="UNAVAILABLE",
                                    reason=f"探测异常：{type(exc).__name__}: {exc}", probed=True, evidence=evidence)

        return capability_status(self.platform_name, capability, status=CAP_NOT_SUPPORTED,
                                reason=f"bilibili 未实现 {capability}", probed=False,
                                evidence={"not_probed_reason": "适配器未声明该能力"})

    def check_access(self, capability: str = "", *, live: bool = True) -> AccessResult:
        """能力检查。第五轮起状态来自**真实探测**，不再硬编码。

        返回 ``AccessResult``（一次调用视角），同时把运行时探测结果放入 ``evidence``，
        便于统一记录"平台 / 接入模式 / 能力 / 来源 / 失败原因 / 检查时间"。
        """
        target = capability or CAP_CONTENT_DETAIL
        probe = self.probe_capability(target, live=live)
        if probe.status == CAP_AVAILABLE:
            return AccessResult(
                self.platform_name, target, ACCESS_OK, probe.reason,
                access_mode=probe.access_mode, evidence=probe.evidence,
            )
        if probe.status == CAP_RATE_LIMITED:
            return AccessResult(
                self.platform_name, target, RATE_LIMITED, probe.reason,
                access_mode=MODE_PUBLIC_READONLY, evidence=probe.evidence,
            )
        if probe.status == CAP_AUTH_REQUIRED:
            return AccessResult(
                self.platform_name, target, AUTH_REQUIRED, probe.reason,
                access_mode=probe.access_mode, evidence=probe.evidence,
            )
        if probe.status == CAP_BLOCKED:
            return AccessResult(
                self.platform_name, target, "PERMISSION_DENIED", probe.reason,
                access_mode=probe.access_mode, evidence=probe.evidence,
            )
        if probe.status == CAP_NOT_SUPPORTED:
            return not_supported(self.platform_name, target, probe.reason)
        return AccessResult(
            self.platform_name, target, UNAVAILABLE, probe.reason,
            access_mode=probe.access_mode, evidence=probe.evidence,
        )

    # ------------------------------------------------------------------ #
    # 能力实现
    # ------------------------------------------------------------------ #
    def search_content(self, keyword: str, limit: int = 10) -> AccessResult:
        if not keyword.strip():
            return AccessResult(self.platform_name, CAP_SEARCH_CONTENT, "INVALID_INPUT", "关键词为空")
        url = (
            f"{API_ROOT}/x/web-interface/search/type?search_type=video"
            f"&keyword={urllib.parse.quote(keyword)}&page=1"
        )
        # 先用探测层判定：搜索接口受 B站风控影响，412 必须如实上报为 RATE_LIMITED。
        status, code, message = self._probe_json(url)
        evidence = {"endpoint": url, "http_status": status, "api_code": code, "keyword": keyword}
        if status == 412 or status == 429:
            return AccessResult(
                self.platform_name, CAP_SEARCH_CONTENT, RATE_LIMITED,
                (f"搜索接口触发风控 HTTP {status}。已立即停止请求，不换 IP / 不换 UA / 不伪造参数。"
                 "降级建议：改用明确视频链接（BV号）直接采集详情与评论，或先用本地已有数据。"),
                access_mode=MODE_PUBLIC_READONLY, evidence=evidence,
            )
        if status in (401, 403):
            return AccessResult(
                self.platform_name, CAP_SEARCH_CONTENT, "PERMISSION_DENIED",
                f"搜索接口拒绝访客访问 HTTP {status}（需登录态，不绕过）",
                access_mode=MODE_PUBLIC_READONLY, evidence=evidence,
            )
        if status != 200 or code != 0:
            return AccessResult(
                self.platform_name, CAP_SEARCH_CONTENT, UNAVAILABLE,
                f"搜索接口不可用：HTTP {status} / code={code} {message}",
                access_mode=MODE_PUBLIC_READONLY, evidence=evidence,
            )
        data = self._get_json(url)
        raw_items = list(data.get("result") or [])[: max(1, limit)]
        items = [self._normalize_search_item(item) for item in raw_items]
        return AccessResult(
            self.platform_name, CAP_SEARCH_CONTENT, "OK",
            f"返回 {len(items)} 条真实结果",
            {"items": [self._dump(item) for item in items], "keyword": keyword},
            access_mode=MODE_PUBLIC_READONLY, evidence=evidence,
        )

    def get_content_detail(self, content_id: str) -> AccessResult:
        try:
            resolved = self._resolve(content_id)
            data = self._get_json(f"{API_ROOT}/x/web-interface/view?bvid={resolved['bvid']}")
        except ValueError as exc:
            return AccessResult(self.platform_name, CAP_CONTENT_DETAIL, "INVALID_INPUT", str(exc))
        except RuntimeError as exc:
            return AccessResult(self.platform_name, CAP_CONTENT_DETAIL, UNAVAILABLE, str(exc))
        except Exception as exc:
            return AccessResult(self.platform_name, CAP_CONTENT_DETAIL, UNAVAILABLE, f"{type(exc).__name__}: {exc}")
        content = self.normalize_content(data)
        return AccessResult(
            self.platform_name, CAP_CONTENT_DETAIL, "OK", "视频详情获取成功",
            {"content": self._dump(content), "aid": resolved["aid"], "cid": resolved["cid"]},
            access_mode=MODE_PUBLIC_READONLY,
            evidence={"endpoint": f"{API_ROOT}/x/web-interface/view?bvid={resolved['bvid']}",
                      "http_status": 200, "sample_bvid": resolved["bvid"]},
        )

    def list_comments(self, content_id: str, limit: int = 30) -> AccessResult:
        try:
            resolved = self._resolve(content_id)
        except (ValueError, RuntimeError) as exc:
            return AccessResult(self.platform_name, CAP_COMMENTS, "INVALID_INPUT", str(exc))
        comments: List[UnifiedComment] = []
        page = 1
        declared_total = 0
        try:
            while len(comments) < max(1, limit):
                data = self._get_json(
                    f"{API_ROOT}/x/v2/reply?type=1&oid={resolved['aid']}&pn={page}&ps=20&sort=2"
                )
                page_info = data.get("page") or {}
                declared_total = int(page_info.get("count") or declared_total or 0)
                batch = data.get("replies") or []
                if not batch:
                    break
                for raw in batch:
                    comments.append(self.normalize_comment(raw, platform_content_id=resolved["bvid"]))
                    if len(comments) >= limit:
                        break
                page += 1
                if page > 20:  # 硬上限，防止无限翻页
                    break
        except RuntimeError as exc:
            return AccessResult(self.platform_name, CAP_COMMENTS, UNAVAILABLE, str(exc))
        except Exception as exc:
            return AccessResult(self.platform_name, CAP_COMMENTS, UNAVAILABLE, f"{type(exc).__name__}: {exc}")

        # 平台侧软限流：接口 code=0 但返回的评论远少于声明总数。
        # 如实标注"部分返回"，不做任何绕过（不换 IP / 不换 UA / 不重试）。
        partial = declared_total > 0 and len(comments) < min(declared_total, max(1, limit))
        message = f"返回 {len(comments)} 条一级评论"
        if partial:
            message += (f"（平台声明 {declared_total} 条，实际仅返回 {len(comments)} 条："
                        "疑似平台侧限流，已如实标注为部分返回，未做绕过）")
        return AccessResult(
            self.platform_name, CAP_COMMENTS, "OK", message,
            {"comments": [self._dump(c) for c in comments], "content_id": resolved["bvid"],
             "partial": partial, "declared_total": declared_total},
            access_mode=MODE_PUBLIC_READONLY,
            evidence={"endpoint": f"{API_ROOT}/x/v2/reply?type=1&oid={resolved['aid']}",
                      "http_status": 200, "sample_aid": resolved["aid"],
                      "count": len(comments), "declared_total": declared_total,
                      "partial": partial},
        )

    def list_replies(self, content_id: str, parent_comment_id: str, limit: int = 20) -> AccessResult:
        try:
            resolved = self._resolve(content_id)
            data = self._get_json(
                f"{API_ROOT}/x/v2/reply/reply?type=1&oid={resolved['aid']}&root={parent_comment_id}&pn=1&ps={max(1, limit)}"
            )
        except ValueError as exc:
            return AccessResult(self.platform_name, CAP_REPLIES, "INVALID_INPUT", str(exc))
        except RuntimeError as exc:
            return AccessResult(self.platform_name, CAP_REPLIES, UNAVAILABLE, str(exc))
        except Exception as exc:
            return AccessResult(self.platform_name, CAP_REPLIES, UNAVAILABLE, f"{type(exc).__name__}: {exc}")
        replies = [
            self.normalize_comment(raw, platform_content_id=resolved["bvid"])
            for raw in (data.get("replies") or [])
        ]
        return AccessResult(
            self.platform_name, CAP_REPLIES, "OK", f"返回 {len(replies)} 条二级评论",
            {"comments": [self._dump(c) for c in replies], "parent_comment_id": parent_comment_id},
            access_mode=MODE_PUBLIC_READONLY,
            evidence={"endpoint": f"{API_ROOT}/x/v2/reply/reply?type=1&oid={resolved['aid']}",
                      "http_status": 200, "parent_comment_id": parent_comment_id},
        )

    def collect_barrage(self, content_id: str, limit: int = 200) -> AccessResult:
        try:
            resolved = self._resolve(content_id)
            status, raw = self._request(f"{DANMAKU_ROOT}/{resolved['cid']}.xml", accept="application/xml")
        except ValueError as exc:
            return AccessResult(self.platform_name, CAP_BARRAGE, "INVALID_INPUT", str(exc))
        except Exception as exc:
            return AccessResult(self.platform_name, CAP_BARRAGE, UNAVAILABLE, f"{type(exc).__name__}: {exc}")
        if status != 200:
            return AccessResult(self.platform_name, CAP_BARRAGE, UNAVAILABLE, f"弹幕接口 HTTP {status}")
        body = self._decode(raw).decode("utf-8", "replace")
        pairs = re.findall(r'<d p="([^"]+)">([^<]*)</d>', body)[: max(1, limit)]
        items = []
        for attrs, text in pairs:
            fields = attrs.split(",")
            items.append({
                "text": text,
                "time_offset": fields[0] if fields else "",
                "mode": fields[1] if len(fields) > 1 else "",
                "color": fields[3] if len(fields) > 3 else "",
                "platform_content_id": resolved["bvid"],
            })
        return AccessResult(
            self.platform_name, CAP_BARRAGE, "OK", f"返回 {len(items)} 条弹幕",
            {"barrage": items, "content_id": resolved["bvid"]},
            access_mode=MODE_PUBLIC_READONLY,
            evidence={"endpoint": f"{DANMAKU_ROOT}/{resolved['cid']}.xml",
                      "http_status": 200, "sample_cid": resolved["cid"], "count": len(items)},
        )

    def list_creator_content(self, creator_id: str, limit: int = 20) -> AccessResult:
        # 需要 wbi 签名与登录态，当前不声明、不伪造。
        return not_supported(self.platform_name, CAP_CREATOR_CONTENT, "UP主作品列表需 wbi 签名/登录态，未实现")

    # ------------------------------------------------------------------ #
    # 归一化
    # ------------------------------------------------------------------ #
    def normalize_content(self, raw: dict) -> UnifiedContent:
        stat = raw.get("stat") or {}
        return UnifiedContent(
            platform=self.platform_name,
            platform_content_id=str(raw.get("bvid") or ""),
            content_type="video",
            title=str(raw.get("title") or ""),
            text=str(raw.get("desc") or ""),
            source_url=f"https://www.bilibili.com/video/{raw.get('bvid')}" if raw.get("bvid") else "",
            author_ref=str((raw.get("owner") or {}).get("name") or ""),
            published_at=_ts_to_iso(raw.get("pubdate")),
            engagement_metrics={
                "view": stat.get("view"),
                "danmaku": stat.get("danmaku"),
                "reply": stat.get("reply"),
                "like": stat.get("like"),
                "coin": stat.get("coin"),
                "favorite": stat.get("favorite"),
                "share": stat.get("share"),
            },
            raw_metadata={
                "aid": raw.get("aid"),
                "cid": raw.get("cid"),
                "duration": raw.get("duration"),
                "tid": raw.get("tid"),
                "tname": raw.get("tname"),
                "pic": raw.get("pic"),
                "owner_mid": (raw.get("owner") or {}).get("mid"),
            },
            source_method="bilibili_public_api",
        )

    def normalize_comment(self, raw: dict, *, platform_content_id: str = "") -> UnifiedComment:
        root = str(raw.get("root") or 0)
        parent = str(raw.get("parent") or 0)
        # root==parent==0 -> 一级评论；否则 parent 指向被回复的评论
        parent_id = "" if (root in ("0", "") and parent in ("0", "")) else (parent if parent not in ("0", "") else root)
        return UnifiedComment(
            platform=self.platform_name,
            platform_comment_id=str(raw.get("rpid") or ""),
            platform_content_id=platform_content_id,
            content=str((raw.get("content") or {}).get("message") or ""),
            parent_comment_id=parent_id,
            author_ref=str((raw.get("member") or {}).get("uname") or ""),
            published_at=_ts_to_iso((raw.get("ctime"))),
            engagement_metrics={"like": raw.get("like"), "reply_count": raw.get("rcount")},
            provenance={"source_method": "bilibili_public_api", "collected_at": _now_iso()},
        )

    def _normalize_search_item(self, raw: dict) -> UnifiedContent:
        title = re.sub(r"</?em[^>]*>", "", str(raw.get("title") or ""))
        return UnifiedContent(
            platform=self.platform_name,
            platform_content_id=str(raw.get("bvid") or ""),
            content_type="video",
            title=title,
            text=str(raw.get("description") or ""),
            source_url=f"https://www.bilibili.com/video/{raw.get('bvid')}" if raw.get("bvid") else "",
            author_ref=str(raw.get("author") or ""),
            published_at=_ts_to_iso(raw.get("pubdate")),
            engagement_metrics={
                "view": raw.get("play"),
                "danmaku": raw.get("video_review"),
                "like": raw.get("like"),
                "favorite": raw.get("favorites"),
            },
            raw_metadata={"aid": raw.get("aid"), "duration": raw.get("duration")},
            source_method="bilibili_public_api_search",
        )

    # ------------------------------------------------------------------ #
    # 旧 ABC 兼容层（供既有 crawl 流程使用）
    # ------------------------------------------------------------------ #
    def search(self, seed: str, limit: int = 10) -> Iterable[SearchResult]:
        result = self.search_content(seed, limit=limit)
        items = result.detail.get("items") or []
        for item in items:
            yield SearchResult(
                platform=self.platform_name,
                source_url=item.get("source_url") or "",
                title=item.get("title") or "",
                content_id=item.get("platform_content_id") or "",
                author_name=item.get("author_ref") or "",
                raw_metadata={"engagement": item.get("engagement_metrics") or {}},
            )

    def fetch_item(self, url_or_id: str) -> ContentItem:
        result = self.get_content_detail(url_or_id)
        if not result.ok:
            raise RuntimeError(f"bilibili fetch_item failed: {result.status} {result.message}")
        item = result.detail["content"]
        metrics = item.get("engagement_metrics") or {}
        return ContentItem(
            platform=self.platform_name,
            source_url=item.get("source_url") or "",
            content_id=item.get("platform_content_id") or "",
            author_name=item.get("author_ref") or "",
            title=item.get("title") or "",
            body=item.get("text") or "",
            publish_time=item.get("published_at") or "",
            engagement=EngagementMetrics(
                view_count=str(metrics.get("view") or ""),
                comment_count=str(metrics.get("reply") or ""),
                like_count=str(metrics.get("like") or ""),
                favorite_count=str(metrics.get("favorite") or ""),
                share_count=str(metrics.get("share") or ""),
            ),
            raw_metadata=item.get("raw_metadata") or {},
        )

    def fetch_comments(self, item_id: str, limit: int = 30) -> List[ContentComment]:
        result = self.list_comments(item_id, limit=limit)
        if not result.ok:
            return []
        out: List[ContentComment] = []
        for comment in result.detail.get("comments") or []:
            out.append(
                ContentComment(
                    platform=self.platform_name,
                    content_id=comment.get("platform_content_id") or "",
                    comment_id=comment.get("platform_comment_id") or "",
                    username=comment.get("author_ref") or "",
                    content=comment.get("content") or "",
                    raw_text=comment.get("content") or "",
                    publish_time=comment.get("published_at") or "",
                    likes=str((comment.get("engagement_metrics") or {}).get("like") or ""),
                    raw_metadata=comment.get("provenance") or {},
                )
            )
        return out

    def normalize(self, raw: dict) -> ContentItem:
        return self.fetch_item(str(raw.get("bvid") or raw.get("content_id") or ""))

    # ------------------------------------------------------------------ #
    @staticmethod
    def _dump(obj: Any) -> Dict[str, Any]:
        if isinstance(obj, UnifiedContent):
            return {
                "platform": obj.platform,
                "platform_content_id": obj.platform_content_id,
                "content_type": obj.content_type,
                "title": obj.title,
                "text": obj.text,
                "source_url": obj.source_url,
                "author_ref": obj.author_ref,
                "published_at": obj.published_at,
                "engagement_metrics": dict(obj.engagement_metrics),
                "raw_metadata": dict(obj.raw_metadata),
                "source_method": obj.source_method,
            }
        if isinstance(obj, UnifiedComment):
            return {
                "platform": obj.platform,
                "platform_comment_id": obj.platform_comment_id,
                "platform_content_id": obj.platform_content_id,
                "parent_comment_id": obj.parent_comment_id,
                "content": obj.content,
                "author_ref": obj.author_ref,
                "published_at": obj.published_at,
                "engagement_metrics": dict(obj.engagement_metrics),
                "provenance": dict(obj.provenance),
            }
        return dict(obj)  # type: ignore[arg-type]

    def close(self) -> None:
        return None
