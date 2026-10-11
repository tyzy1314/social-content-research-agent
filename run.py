#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单链接调试入口。
推荐主流程请使用 `python search_stream_scrape.py`。
"""

from xhs_scraper import XHSCommentScraper
from datetime import datetime

def main():
    print("=" * 60)
    print("小红书单链接调试入口")
    print("=" * 60)
    print("\n提示：")
    print("1. 请确保已安装Chrome浏览器")
    print("2. 首次运行会自动下载ChromeDriver（可能需要几分钟）")
    print("3. 建议手动登录小红书后再爬取，成功率更高")
    print("4. 这是调试入口，不是推荐批量主流程\n")

    # 获取用户输入
    note_url = input("请输入小红书笔记链接: ").strip()

    if not note_url:
        print("错误：链接不能为空")
        return

    max_comments_input = input("最大爬取评论数（默认50，直接回车使用默认值）: ").strip()
    max_comments = int(max_comments_input) if max_comments_input.isdigit() else 50

    print(f"\n开始爬取，目标：{max_comments} 条评论")
    print("=" * 60)

    # 创建爬虫实例（非无头模式，可以看到浏览器操作）
    scraper = XHSCommentScraper(headless=False)

    try:
        # 设置浏览器
        print("\n[1/4] 正在启动浏览器...")
        scraper.setup_driver()

        print("[2/4] 正在访问页面...")
        print("\n提示：如果出现登录、短信验证码、安全验证，请在浏览器中手动完成")
        print("确认已经看到笔记正文和评论区后，再回到终端按 Enter 继续。\n")

        # 爬取评论
        print("[3/4] 正在爬取评论...")
        comments = scraper.scrape_comments(note_url, max_comments=max_comments, scroll_pause=3)

        # 保存数据
        if comments:
            print(f"\n[4/4] 正在保存数据...")
            filename = f"xhs_comments_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            scraper.save_to_csv(filename)

            print("\n" + "=" * 60)
            print(f"爬取完成！")
            print(f"成功获取: {len(comments)} 条评论")
            print(f"保存位置: {filename}")
            print("=" * 60)
        else:
            print("\n警告：未获取到评论数据")
            print("可能的原因：")
            print("- 该笔记没有评论")
            print("- 需要登录才能查看评论")
            print("- debug_page.html 里仍是登录页或验证码页")
            print("- 页面结构已变化，需要更新选择器")
            print("- 触发了反爬虫机制")

    except KeyboardInterrupt:
        print("\n\n用户中断操作")
    except Exception as e:
        print(f"\n错误：{e}")
        print("\n如果遇到问题，请检查：")
        print("1. Chrome浏览器是否已安装")
        print("2. 网络连接是否正常")
        print("3. 笔记链接是否正确")
    finally:
        print("\n正在关闭浏览器...")
        scraper.close()
        print("完成！")

if __name__ == "__main__":
    main()
