#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键运行脚本 - 从爬取到报告生成的完整流程
"""

from xhs_scraper import XHSCommentScraper
from sentiment_analyzer import SentimentAnalyzer
from report_generator import ReportGenerator
from datetime import datetime
import sys


def run_complete_workflow():
    """运行完整的工作流程"""
    print("=" * 70)
    print("多平台内容采集与分析框架 - 历史完整流程")
    print("=" * 70)
    print()

    # 步骤1: 爬取评论
    print("【步骤 1/3】爬取评论数据")
    print("-" * 70)

    note_url = input("请输入小红书笔记链接: ").strip()
    if not note_url:
        print("错误: 链接不能为空")
        return

    max_comments_input = input("最大爬取评论数（默认100）: ").strip()
    max_comments = int(max_comments_input) if max_comments_input.isdigit() else 100

    print(f"\n开始爬取，目标: {max_comments} 条评论")

    scraper = XHSCommentScraper(headless=False)

    try:
        scraper.setup_driver()
        comments = scraper.scrape_comments(note_url, max_comments=max_comments, scroll_pause=2)

        if not comments:
            print("未获取到评论数据，流程终止")
            return

        # 保存CSV
        csv_filename = f"xhs_comments_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        scraper.save_to_csv(csv_filename)
        print(f"✓ 成功爬取 {len(comments)} 条评论，已保存到: {csv_filename}")

    except Exception as e:
        print(f"爬取失败: {e}")
        return
    finally:
        scraper.close()

    print()

    # 步骤2: 数据分析
    print("【步骤 2/3】分析数据")
    print("-" * 70)

    analyzer = SentimentAnalyzer(csv_filename)
    analysis_result = analyzer.run_full_analysis()

    if not analysis_result:
        print("数据分析失败，流程终止")
        return

    print()
    print(analyzer.get_summary())
    print()

    # 步骤3: 生成PDF报告
    print("【步骤 3/3】生成PDF报告")
    print("-" * 70)

    pdf_filename = f"舆情报告_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    generator = ReportGenerator(analysis_result, pdf_filename)

    if generator.generate():
        print()
        print("=" * 70)
        print("✓ 全部完成！")
        print(f"  - CSV数据: {csv_filename}")
        print(f"  - PDF报告: {pdf_filename}")
        print("=" * 70)
    else:
        print("PDF报告生成失败")


def main():
    """主函数"""
    try:
        run_complete_workflow()
    except KeyboardInterrupt:
        print("\n\n用户中断操作")
    except Exception as e:
        print(f"\n发生错误: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
