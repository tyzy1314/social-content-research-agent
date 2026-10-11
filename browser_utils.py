#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
浏览器与通用链接工具。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager

PROJECT_ROOT = Path(__file__).resolve().parent
CHROME_BINARY = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
FALLBACK_CHROMEDRIVER = (
    PROJECT_ROOT
    / "chromedriver"
    / "win64-147.0.7727.102"
    / "chromedriver-win64"
    / "chromedriver.exe"
)
BASE_URL = "https://www.xiaohongshu.com"
LOGIN_BLOCK_KEYWORDS = [
    "登录",
    "验证码",
    "短信验证码",
    "输入手机号",
    "login-modal",
    "login-container",
    "fe-captcha",
]


def get_chrome_user_data_dir() -> Path:
    """Return the local Chrome profile path without embedding a user's home path."""
    configured_path = os.environ.get("XHS_CHROME_PROFILE")
    if not configured_path:
        return Path.home() / "Desktop" / "xhs_chrome_profile"

    path = Path(configured_path).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


UNAVAILABLE_URL_MARKERS = [
    "/404",
    "source=/404",
    "error_code=",
    "redirectpath=",
]
UNAVAILABLE_PAGE_MARKERS = [
    "当前笔记暂时无法浏览",
    "请打开小红书App扫码查看",
    "页面不存在",
    "访问的页面不见了",
    "暂时无法浏览",
    "app扫码查看",
    "扫码查看",
]
COMMENT_AREA_SELECTORS = [
    ".comments-container",
    '[class*="comments-container"]',
    '[class*="comment-container"]',
    '[class*="comment-list"]',
    '[class*="parent-comment"]',
    '[class*="comment-inner"]',
    '[class*="comment-item"]',
    'div[class*="comment"]',
]
UNAVAILABLE_REASON = "网页端不可访问或 App-only"
AVAILABLE_NOTE_SELECTORS = [
    '[class*="note"]',
    '[class*="comments"]',
    ".comments-container",
    '[class*="comment"]',
    '[class*="interaction"]',
]
AVAILABLE_TEXT_MARKERS = ["评论", "赞", "收藏"]


def sanitize_filename(value: str, default: str = "xhs") -> str:
    """清洗 Windows 文件名中的非法字符。"""
    cleaned = "".join("_" if char in '<>:"/\\|?*' else char for char in value.strip())
    cleaned = cleaned.strip(" .")
    return cleaned or default


def page_contains_block_keywords(page_source: str) -> bool:
    """判断页面源码中是否仍包含登录/验证相关关键字。"""
    source = (page_source or "").lower()
    return any(keyword.lower() in source for keyword in LOGIN_BLOCK_KEYWORDS)


def is_unavailable_note_page(driver, debug_filename: str = "debug_page.html") -> tuple[bool, str]:
    """
    判断当前笔记是否为网页端不可访问或仅支持 App 查看的页面。
    检测异常时返回 (False, "")，不抛出异常。
    """
    try:
        page_source = driver.page_source or ""
    except Exception:
        page_source = ""

    try:
        Path(debug_filename).write_text(page_source, encoding="utf-8")
    except Exception:
        pass

    try:
        current_url = (driver.current_url or "").strip()
    except Exception:
        current_url = ""

    try:
        page_text = driver.find_element(By.TAG_NAME, "body").text or ""
    except Exception:
        page_text = ""

    try:
        url_lower = current_url.lower()
        combined_text = f"{page_text}\n{page_source}"
        combined_text_lower = combined_text.lower()

        if any(marker in url_lower for marker in UNAVAILABLE_URL_MARKERS):
            return True, UNAVAILABLE_REASON

        if any(marker.lower() in combined_text_lower for marker in UNAVAILABLE_PAGE_MARKERS):
            return True, UNAVAILABLE_REASON
    except Exception:
        return False, ""

    return False, ""


def has_comment_area(driver) -> bool:
    """优先根据 Selenium 可见元素判断页面里是否存在评论区或评论元素。"""
    for selector in COMMENT_AREA_SELECTORS:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            continue

        for element in elements:
            try:
                if not element.is_displayed():
                    continue
                rect = element.rect or {}
                if rect.get("width", 0) <= 0 or rect.get("height", 0) <= 0:
                    continue
                text = (element.text or "").strip()
                children = element.find_elements(By.CSS_SELECTOR, "*")
                if text or children:
                    return True
            except Exception:
                continue
    return False


def is_available_note_page(driver) -> bool:
    """
    判断当前页面是否为真正可访问的笔记页。
    优先依赖 Selenium 可见元素，其次结合正文文本中的交互词判断。
    """
    try:
        unavailable, _ = is_unavailable_note_page(driver, debug_filename="debug_page.html")
        if unavailable:
            return False
    except Exception:
        return False

    try:
        current_url = (driver.current_url or "").strip().lower()
    except Exception:
        current_url = ""

    visible_hits = 0
    for selector in AVAILABLE_NOTE_SELECTORS:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            continue

        for element in elements:
            try:
                if not element.is_displayed():
                    continue
                rect = element.rect or {}
                if rect.get("width", 0) <= 0 or rect.get("height", 0) <= 0:
                    continue
                text = (element.text or "").strip()
                has_media = bool(element.find_elements(By.CSS_SELECTOR, "img, video, button, svg"))
                if text or has_media:
                    visible_hits += 1
                    break
            except Exception:
                continue

    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text or ""
    except Exception:
        body_text = ""

    text_hits = sum(1 for marker in AVAILABLE_TEXT_MARKERS if marker in body_text)

    if visible_hits >= 1 and ("/explore/" in current_url or text_hits >= 1):
        return True

    if text_hits >= 2 and "/explore/" in current_url:
        return True

    return False


def canonicalize_explore_url(url: str) -> Optional[str]:
    """标准化笔记链接，只保留 explore 主路径。"""
    if not url:
        return None

    normalized = urljoin(BASE_URL, url.strip())
    parsed = urlparse(normalized)
    if "xiaohongshu.com" not in parsed.netloc:
        return None
    if "/explore/" not in parsed.path:
        return None

    note_id = parsed.path.split("/explore/", 1)[1].split("/", 1)[0].strip()
    if not note_id:
        return None

    return f"{BASE_URL}/explore/{note_id}"


def _build_chrome_options(headless: bool = False) -> Options:
    options = Options()

    if not CHROME_BINARY.exists():
        raise FileNotFoundError(
            f"未找到 Chrome 浏览器：{CHROME_BINARY}，请确认 Chrome 已安装。"
        )

    user_data_dir = get_chrome_user_data_dir()
    user_data_dir.mkdir(parents=True, exist_ok=True)

    if headless:
        options.add_argument("--headless=new")

    options.binary_location = str(CHROME_BINARY)
    options.add_argument(f"--user-data-dir={user_data_dir}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--lang=zh-CN")
    return options


def create_chrome_driver(headless: bool = False) -> webdriver.Chrome:
    """
    创建 Chrome Driver。
    优先使用 webdriver_manager，失败后回退到本地 chromedriver。
    """
    options = _build_chrome_options(headless=headless)

    try:
        service = Service(ChromeDriverManager().install())
        print("已使用 webdriver_manager 准备 ChromeDriver。")
        return webdriver.Chrome(service=service, options=options)
    except Exception as download_error:
        print(f"webdriver_manager 启动失败，将尝试本地 chromedriver：{download_error}")

    if not FALLBACK_CHROMEDRIVER.exists():
        raise FileNotFoundError(
            "webdriver_manager 启动失败，且本地 chromedriver 不存在："
            f"{FALLBACK_CHROMEDRIVER}"
        )

    try:
        print(f"使用本地 chromedriver：{FALLBACK_CHROMEDRIVER}")
        service = Service(str(FALLBACK_CHROMEDRIVER))
        return webdriver.Chrome(service=service, options=options)
    except Exception as fallback_error:
        raise RuntimeError(
            "webdriver_manager 和本地 chromedriver 均无法启动，请检查 Chrome 与驱动版本。"
        ) from fallback_error
