#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小红书评论爬虫 - 命令行版本
使用方法: python scrape.py <笔记链接> [最大评论数]
示例: python scrape.py https://www.xiaohongshu.com/explore/xxxxx 200
"""

import sys
from xhs_scraper import XHSCommentScraper
from datetime import datetime

def main():
    # 检查命令行参数
    if len(sys.argv) < 2:
        print("使用方法: python scrape.py <笔记链接> [最大评论数]")
        print("示例: python scrape.py https://www.xiaohongshu.com/explore/xxxxx 200")
        sys.exit(1)

    note_url = sys.argv[1]
    max_comments = int(sys.argv[2]) if len(sys.argv) > 2 else 100

    print("=" * 60)
    print("小红书评论爬虫工具")
    print("=" * 60)
    print(f"目标链接: {note_url}")
    print(f"最大评论数: {max_comments}")
    print("=" * 60)

    # 创建爬虫实例
    scraper = XHSCommentScraper(headless=False)

    try:
        print("\n[1/4] 正在启动浏览器...")
        scraper.setup_driver()

        print("[2/4] 正在访问页面...")
        print("提示: 如需登录，请在浏览器中手动登录后等待程序继续\n")

        print("[3/4] 正在爬取评论...")
        comments = scraper.scrape_comments(note_url, max_comments=max_comments, scroll_pause=2, wait_for_login=60)

        if comments:
            print(f"\n[4/4] 正在保存数据...")
            filename = f"xhs_comments_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            scraper.save_to_csv(filename)

            print("\n" + "=" * 60)
            print(f"爬取完成!")
            print(f"成功获取: {len(comments)} 条评论")
            print(f"保存位置: {filename}")
            print("=" * 60)
        else:
            print("\n警告: 未获取到评论数据")
            print("可能原因:")
            print("- 该笔记没有评论")
            print("- 需要登录才能查看评论")
            print("- 页面结构已变化")
            print("- 触发了反爬虫机制")

    except KeyboardInterrupt:
        print("\n\n用户中断操作")
    except Exception as e:
        print(f"\n错误: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("\n正在关闭浏览器...")
        scraper.close()
        print("完成!")

if __name__ == "__main__":
    main()
