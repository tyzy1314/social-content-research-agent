#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分析报告生成主程序。

整合 ``sentiment_analyzer`` 与 ``report_generator``，从本地 CSV 生成 PDF。不会访问外部接口。
"""

from sentiment_analyzer import SentimentAnalyzer
from report_generator import ReportGenerator
import sys
import os


def generate_report(csv_file):
    """生成舆情报告"""
    print("=" * 60)
    print("小红书舆情报告生成器")
    print("=" * 60)
    print()

    # 检查文件是否存在
    if not os.path.exists(csv_file):
        print(f"错误: 文件不存在 - {csv_file}")
        return False

    # 步骤1: 数据分析
    print("步骤 1/2: 正在分析数据...")
    print("-" * 60)
    analyzer = SentimentAnalyzer(csv_file)
    analysis_result = analyzer.run_full_analysis()

    if not analysis_result:
        print("错误: 数据分析失败")
        return False

    print()
    print(analyzer.get_summary())
    print()

    # 步骤2: 生成PDF报告
    print("步骤 2/2: 正在生成PDF报告...")
    print("-" * 60)

    # 生成输出文件名
    base_name = os.path.splitext(os.path.basename(csv_file))[0]
    output_file = f"舆情报告_{base_name}.pdf"

    generator = ReportGenerator(analysis_result, output_file)
    success = generator.generate()

    if success:
        print()
        print("=" * 60)
        print("报告生成完成！")
        print(f"输出文件: {output_file}")
        print("=" * 60)
        return True
    else:
        print("错误: PDF报告生成失败")
        return False


def main():
    """主函数"""
    if len(sys.argv) > 1:
        # 命令行参数模式
        csv_file = sys.argv[1]
    else:
        # 交互模式
        print("请输入CSV文件路径（或直接回车使用最新的CSV文件）:")
        csv_input = input().strip()

        if csv_input:
            csv_file = csv_input
        else:
            # 查找最新的CSV文件
            import glob
            csv_files = glob.glob("xhs_comments_*.csv")
            if csv_files:
                csv_file = max(csv_files, key=os.path.getmtime)
                print(f"使用最新的CSV文件: {csv_file}")
            else:
                print("错误: 未找到CSV文件")
                return

    # 生成报告
    generate_report(csv_file)


if __name__ == "__main__":
    main()
