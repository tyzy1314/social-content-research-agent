#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成免登录接入能力矩阵（第五轮）。

矩阵**由真实运行时探测生成**（live=True），不是手写常量：
* 每个平台每项能力发一次请求；
* 遇到 401/403/412/429 立即停止并记录原因，不重试、不换 IP/UA、不伪造；
* 无合法免登录端点的平台标记"未发起请求"并写明原因。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from platforms import PLATFORM_ORDER, get_adapter  # noqa: E402
from platforms.capabilities import ALL_CAPABILITIES  # noqa: E402

OUT = ROOT / "outputs" / "access-matrix-round5.json"


def main() -> int:
    matrix: dict[str, dict] = {}
    for name in PLATFORM_ORDER:
        try:
            adapter = get_adapter(name)
        except Exception as exc:
            matrix[name] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        info = adapter.platform_info()
        caps = {}
        for capability in ALL_CAPABILITIES:
            status = adapter.probe_capability(capability, live=True)
            caps[capability] = status.as_dict()
        matrix[name] = {
            "display_name": info.display_name,
            "verification": info.verification,
            "auth_required": info.auth_required,
            "access_modes": list(adapter.access_modes()),
            "default_access_mode": adapter.default_access_mode(),
            "login_free_capabilities": list(adapter.login_free_capabilities()),
            "declared_capabilities": list(adapter.capabilities()),
            "capabilities": caps,
        }
        print(f"[probe] {name}: " + ", ".join(
            f"{c}={caps[c]['status']}" for c in ALL_CAPABILITIES if c in caps))

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": ("本矩阵由真实运行时探测生成；状态随时间与平台风控策略变化。"
                 "412/403 等限制信号出现时立即停止请求，不做任何绕过。"),
        "platforms": matrix,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwritten: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
