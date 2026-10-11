#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Legacy 兼容入口。
当前推荐主流程：python search_stream_scrape.py
"""

from legacy.batch_scrape import *  # noqa: F401,F403
from legacy.batch_scrape import main as legacy_main


def main():
    print("Legacy：batch_scrape.py 已不再推荐。请优先使用 python search_stream_scrape.py")
    return legacy_main()


if __name__ == "__main__":
    main()
