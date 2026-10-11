#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""评论数据分析模块。

对多平台采集到的内容评论进行情感分类、热词提取与统计汇总。仅面向本地
已采集的 CSV / SQLite 数据，不会发起外部请求。
"""

import pandas as pd
import re
from collections import Counter
from datetime import datetime
import jieba
import jieba.analyse

# 点赞数单位换算：平台在过万后只显示 "1.2万" / "3.5w" 这类缩写
LIKE_UNIT_MULTIPLIERS = {'万': 10000, 'w': 10000, '亿': 100000000, '千': 1000, 'k': 1000}

# ---------------------------------------------------------------------------
# 可复用的词典基线（从 SentimentAnalyzer 中提取，供 agent 工具层复用，避免重复实现）
# 注意：这是**规则词典基线，不是训练模型**——仅统计词典命中数做正负比较。
# 已知局限：子串匹配无法识别否定、反讽与上下文（例如"不错"含"不"、"没差"含"差"）。
# ---------------------------------------------------------------------------
POSITIVE_WORDS = ('好', '棒', '赞', '喜欢', '支持', '优秀', '厉害', '不错', '感谢', '有用', '推荐')
NEGATIVE_WORDS = ('差', '烂', '垃圾', '骗', '假', '坑', '失望', '后悔', '不行', '问题', '难用')
NEUTRAL_WORDS = ('怎么', '什么', '哪里', '如何', '请问', '吗', '呢')


def classify_sentiment(text: str) -> str:
    """用词典基线对单条评论分类，返回 '正面' / '负面' / '中性'。

    pos > neg 记正面，neg > pos 记负面，其余（无信号或正负抵消）记中性。
    """
    content = str(text).lower()
    pos_count = sum(1 for word in POSITIVE_WORDS if word in content)
    neg_count = sum(1 for word in NEGATIVE_WORDS if word in content)
    if pos_count > neg_count:
        return '正面'
    if neg_count > pos_count:
        return '负面'
    return '中性'


class SentimentAnalyzer:
    def __init__(self, csv_file):
        """初始化分析器"""
        self.csv_file = csv_file
        self.df = None
        self.analysis_result = {}

    def load_data(self):
        """加载CSV数据"""
        try:
            self.df = pd.read_csv(self.csv_file, encoding='utf-8-sig')
            print(f"成功加载 {len(self.df)} 条评论数据")
            return True
        except Exception as e:
            print(f"加载数据失败: {e}")
            return False

    def basic_statistics(self):
        """基础统计分析"""
        if self.df is None:
            return None

        stats = {
            '总评论数': len(self.df),
            '用户数': self.df['用户名'].nunique(),
            '平均评论长度': self.df['评论内容'].str.len().mean(),
            '最长评论长度': self.df['评论内容'].str.len().max(),
            '最短评论长度': self.df['评论内容'].str.len().min(),
        }

        # 点赞数统计
        likes = self.df['点赞数'].apply(self._parse_likes)
        stats['总点赞数'] = likes.sum()
        stats['平均点赞数'] = likes.mean()
        stats['最高点赞数'] = likes.max()

        self.analysis_result['基础统计'] = stats
        return stats

    def _parse_likes(self, like_str):
        """解析点赞数，支持 "1.2万" / "3.5w" 等带单位的缩写写法"""
        if pd.isna(like_str) or like_str == '赞':
            return 0
        text = str(like_str).replace(',', '')
        match = re.search(r'(\d+(?:\.\d+)?)\s*([万亿千wWkK]?)', text)
        if not match:
            return 0
        try:
            value = float(match.group(1))
        except ValueError:
            return 0
        multiplier = LIKE_UNIT_MULTIPLIERS.get(match.group(2).lower(), 1)
        return int(value * multiplier)

    def sentiment_analysis(self):
        """情感分析"""
        if self.df is None:
            return None

        # 简单的情感词典
        positive_words = ['好', '棒', '赞', '喜欢', '支持', '优秀', '厉害', '不错', '感谢', '有用', '推荐']
        negative_words = ['差', '烂', '垃圾', '骗', '假', '坑', '失望', '后悔', '不行', '问题', '难用']
        neutral_words = ['怎么', '什么', '哪里', '如何', '请问', '吗', '呢']

        sentiments = []
        for content in self.df['评论内容']:
            content_str = str(content).lower()

            pos_count = sum(1 for word in positive_words if word in content_str)
            neg_count = sum(1 for word in negative_words if word in content_str)
            neu_count = sum(1 for word in neutral_words if word in content_str)

            if pos_count > neg_count:
                sentiments.append('正面')
            elif neg_count > pos_count:
                sentiments.append('负面')
            else:
                sentiments.append('中性')

        self.df['情感倾向'] = sentiments

        sentiment_stats = {
            '正面评论数': sentiments.count('正面'),
            '负面评论数': sentiments.count('负面'),
            '中性评论数': sentiments.count('中性'),
            '正面占比': f"{sentiments.count('正面')/len(sentiments)*100:.1f}%",
            '负面占比': f"{sentiments.count('负面')/len(sentiments)*100:.1f}%",
            '中性占比': f"{sentiments.count('中性')/len(sentiments)*100:.1f}%",
        }

        self.analysis_result['情感分析'] = sentiment_stats
        return sentiment_stats

    def keyword_extraction(self, top_n=20):
        """关键词提取"""
        if self.df is None:
            return None

        # 合并所有评论内容
        all_text = ' '.join(self.df['评论内容'].astype(str))

        # 使用jieba提取关键词
        keywords = jieba.analyse.extract_tags(all_text, topK=top_n, withWeight=True)

        keyword_list = [{'关键词': word, '权重': f"{weight:.4f}"} for word, weight in keywords]

        self.analysis_result['关键词'] = keyword_list
        return keyword_list

    def hot_comments(self, top_n=10):
        """热门评论分析"""
        if self.df is None:
            return None

        # 解析点赞数
        self.df['点赞数值'] = self.df['点赞数'].apply(self._parse_likes)

        # 按点赞数排序
        hot_df = self.df.nlargest(top_n, '点赞数值')

        hot_comments = []
        for _, row in hot_df.iterrows():
            hot_comments.append({
                '用户名': row['用户名'],
                '评论内容': row['评论内容'][:100] + '...' if len(str(row['评论内容'])) > 100 else row['评论内容'],
                '点赞数': row['点赞数'],
                '发布时间': row['发布时间']
            })

        self.analysis_result['热门评论'] = hot_comments
        return hot_comments

    def user_activity(self, top_n=10):
        """用户活跃度分析"""
        if self.df is None:
            return None

        user_counts = self.df['用户名'].value_counts().head(top_n)

        active_users = [{'用户名': user, '评论数': count} for user, count in user_counts.items()]

        self.analysis_result['活跃用户'] = active_users
        return active_users

    def time_distribution(self):
        """时间分布分析"""
        if self.df is None:
            return None

        time_pattern = r'(\d+)(小时|分钟|天|昨天|今天)'
        time_dist = {'小时内': 0, '天内': 0, '其他': 0}

        for time_str in self.df['发布时间']:
            time_str = str(time_str)
            if '小时' in time_str or '分钟' in time_str:
                time_dist['小时内'] += 1
            elif '天' in time_str or '昨天' in time_str:
                time_dist['天内'] += 1
            else:
                time_dist['其他'] += 1

        self.analysis_result['时间分布'] = time_dist
        return time_dist

    def run_full_analysis(self):
        """运行完整分析"""
        print("开始舆情分析...")

        if not self.load_data():
            return None

        print("1. 基础统计分析...")
        self.basic_statistics()

        print("2. 情感分析...")
        self.sentiment_analysis()

        print("3. 关键词提取...")
        self.keyword_extraction()

        print("4. 热门评论分析...")
        self.hot_comments()

        print("5. 用户活跃度分析...")
        self.user_activity()

        print("6. 时间分布分析...")
        self.time_distribution()

        print("分析完成！")
        return self.analysis_result

    def get_summary(self):
        """生成分析摘要"""
        if not self.analysis_result:
            return "暂无分析结果"

        stats = self.analysis_result.get('基础统计', {})
        sentiment = self.analysis_result.get('情感分析', {})

        summary = f"""
舆情分析摘要：
- 共收集 {stats.get('总评论数', 0)} 条评论，来自 {stats.get('用户数', 0)} 位用户
- 总点赞数 {stats.get('总点赞数', 0)}，平均每条评论获得 {stats.get('平均点赞数', 0):.1f} 个赞
- 情感倾向：正面 {sentiment.get('正面占比', '0%')}，负面 {sentiment.get('负面占比', '0%')}，中性 {sentiment.get('中性占比', '0%')}
"""
        return summary.strip()
