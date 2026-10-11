#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Exact and near-duplicate helpers for collected text."""

from __future__ import annotations

import hashlib
import re
from typing import Iterable, List

TOKEN_PATTERN = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]+")
CJK_PATTERN = re.compile(r"[\u4e00-\u9fff]")
SHINGLE_SIZE = 2
SIMHASH_BITS = 64


def normalize_text(value: object) -> str:
    text = str(value or "").lower()
    tokens = TOKEN_PATTERN.findall(text)
    return " ".join(tokens)


def normalized_text_hash(value: object) -> str:
    normalized = normalize_text(value)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def simhash_text(value: object, *, bits: int = SIMHASH_BITS) -> str:
    tokens = _tokens(value)
    if not tokens:
        return ""

    weights = [0] * bits
    for token in tokens:
        digest = int(hashlib.sha256(token.encode("utf-8")).hexdigest(), 16)
        for bit_index in range(bits):
            bit = (digest >> bit_index) & 1
            weights[bit_index] += 1 if bit else -1

    fingerprint = 0
    for bit_index, weight in enumerate(weights):
        if weight >= 0:
            fingerprint |= 1 << bit_index
    return f"{fingerprint:0{bits // 4}x}"


def simhash_distance(left: str, right: str) -> int:
    if not left or not right:
        return SIMHASH_BITS
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except ValueError:
        return SIMHASH_BITS


def build_content_fingerprint_text(
    *,
    title: object = "",
    body: object = "",
    author_name: object = "",
    publish_time: object = "",
) -> str:
    parts = [title, body, author_name, publish_time]
    return " ".join(str(part or "").strip() for part in parts if str(part or "").strip())


def content_hashes(
    *,
    title: object = "",
    body: object = "",
    author_name: object = "",
    publish_time: object = "",
) -> tuple[str, str]:
    fingerprint_text = build_content_fingerprint_text(
        title=title,
        body=body,
        author_name=author_name,
        publish_time=publish_time,
    )
    return normalized_text_hash(fingerprint_text), simhash_text(fingerprint_text)


def _tokens(value: object) -> List[str]:
    normalized = normalize_text(value)
    if not normalized:
        return []
    tokens: List[str] = []
    for token in normalized.split(" "):
        if not token:
            continue
        if CJK_PATTERN.search(token):
            # 中文没有空格分词，整段中文会变成单个 token，SimHash 就退化成精确哈希；
            # 这里把含中文的 token 再切成字符 2-gram，让近似重复的指纹距离回到可用范围。
            tokens.extend(_char_shingles(token))
        else:
            tokens.append(token)
    return tokens


def _char_shingles(token: str, size: int = SHINGLE_SIZE) -> List[str]:
    if len(token) <= size:
        return [token]
    return [token[index : index + size] for index in range(len(token) - size + 1)]


def first_non_empty(values: Iterable[object]) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""
