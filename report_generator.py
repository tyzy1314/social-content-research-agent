#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF 分析报告生成器。

将 ``sentiment_analyzer`` 输出的评论分析结果（热词 / 情感占比 / 典型评论）渲染为 PDF。
平台中立：只依赖 ``analysis_result`` dict，不耦合具体采集平台。
"""

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from datetime import datetime
import os


class ReportGenerator:
    def __init__(self, analysis_result, output_file=None):
        """初始化报告生成器"""
        self.analysis_result = analysis_result
        self.output_file = output_file or f"舆情报告_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        self.story = []

        # 注册中文字体
        self._register_fonts()

        # 创建样式
        self.styles = getSampleStyleSheet()
        self._create_custom_styles()

    def _register_fonts(self):
        """注册中文字体"""
        try:
            # 尝试使用系统字体
            font_paths = [
                'C:/Windows/Fonts/simhei.ttf',  # 黑体
                'C:/Windows/Fonts/simsun.ttc',  # 宋体
                'C:/Windows/Fonts/msyh.ttc',    # 微软雅黑
            ]

            for font_path in font_paths:
                if os.path.exists(font_path):
                    pdfmetrics.registerFont(TTFont('Chinese', font_path))
                    print(f"成功注册字体: {font_path}")
                    return

            print("警告: 未找到中文字体，将使用默认字体")
        except Exception as e:
            print(f"注册字体失败: {e}")

    def _create_custom_styles(self):
        """创建自定义样式"""
        # 标题样式
        self.title_style = ParagraphStyle(
            'CustomTitle',
            parent=self.styles['Heading1'],
            fontName='Chinese',
            fontSize=24,
            textColor=colors.HexColor('#1a1a1a'),
            spaceAfter=30,
            alignment=1  # 居中
        )

        # 一级标题
        self.heading1_style = ParagraphStyle(
            'CustomHeading1',
            parent=self.styles['Heading1'],
            fontName='Chinese',
            fontSize=16,
            textColor=colors.HexColor('#2c3e50'),
            spaceAfter=12,
            spaceBefore=12
        )

        # 二级标题
        self.heading2_style = ParagraphStyle(
            'CustomHeading2',
            parent=self.styles['Heading2'],
            fontName='Chinese',
            fontSize=14,
            textColor=colors.HexColor('#34495e'),
            spaceAfter=10,
            spaceBefore=10
        )

        # 正文样式
        self.body_style = ParagraphStyle(
            'CustomBody',
            parent=self.styles['BodyText'],
            fontName='Chinese',
            fontSize=11,
            textColor=colors.HexColor('#333333'),
            spaceAfter=6,
            leading=18
        )

    def add_title(self):
        """添加报告标题"""
        title = Paragraph("小红书舆情分析报告", self.title_style)
        self.story.append(title)

        # 添加生成时间
        time_text = f"报告生成时间: {datetime.now().strftime('%Y年%m月%d日 %H:%M:%S')}"
        time_para = Paragraph(time_text, self.body_style)
        self.story.append(time_para)
        self.story.append(Spacer(1, 0.5*cm))

    def add_summary(self):
        """添加概述部分"""
        self.story.append(Paragraph("一、数据概述", self.heading1_style))

        stats = self.analysis_result.get('基础统计', {})

        summary_data = [
            ['指标', '数值'],
            ['总评论数', f"{stats.get('总评论数', 0)} 条"],
            ['参与用户数', f"{stats.get('用户数', 0)} 人"],
            ['总点赞数', f"{stats.get('总点赞数', 0)} 个"],
            ['平均点赞数', f"{stats.get('平均点赞数', 0):.1f} 个/条"],
            ['最高点赞数', f"{stats.get('最高点赞数', 0)} 个"],
            ['平均评论长度', f"{stats.get('平均评论长度', 0):.1f} 字"],
        ]

        table = Table(summary_data, colWidths=[8*cm, 8*cm])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#3498db')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, -1), 'Chinese'),
            ('FONTSIZE', (0, 0), (-1, 0), 12),
            ('FONTSIZE', (0, 1), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#ecf0f1')),
            ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#bdc3c7'))
        ]))

        self.story.append(table)
        self.story.append(Spacer(1, 0.5*cm))

    def add_sentiment_analysis(self):
        """添加情感分析部分"""
        self.story.append(Paragraph("二、情感倾向分析", self.heading1_style))

        sentiment = self.analysis_result.get('情感分析', {})

        sentiment_data = [
            ['情感类型', '数量', '占比'],
            ['正面评论', f"{sentiment.get('正面评论数', 0)} 条", sentiment.get('正面占比', '0%')],
            ['负面评论', f"{sentiment.get('负面评论数', 0)} 条", sentiment.get('负面占比', '0%')],
            ['中性评论', f"{sentiment.get('中性评论数', 0)} 条", sentiment.get('中性占比', '0%')],
        ]

        table = Table(sentiment_data, colWidths=[5*cm, 5*cm, 5*cm])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2ecc71')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, -1), 'Chinese'),
            ('FONTSIZE', (0, 0), (-1, 0), 12),
            ('FONTSIZE', (0, 1), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (0, 1), colors.HexColor('#d5f4e6')),
            ('BACKGROUND', (0, 2), (0, 2), colors.HexColor('#fadbd8')),
            ('BACKGROUND', (0, 3), (0, 3), colors.HexColor('#fef5e7')),
            ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#bdc3c7'))
        ]))

        self.story.append(table)
        self.story.append(Spacer(1, 0.3*cm))

        # 添加情感分析结论
        pos_ratio = float(sentiment.get('正面占比', '0%').rstrip('%'))
        neg_ratio = float(sentiment.get('负面占比', '0%').rstrip('%'))

        if pos_ratio > 60:
            conclusion = "整体舆情呈现<b>积极正面</b>态势，用户满意度较高。"
        elif neg_ratio > 40:
            conclusion = "存在较多<b>负面评论</b>，需要关注用户反馈并及时改进。"
        else:
            conclusion = "舆情整体<b>较为平稳</b>，正负面评论相对均衡。"

        self.story.append(Paragraph(f"<b>分析结论：</b>{conclusion}", self.body_style))
        self.story.append(Spacer(1, 0.5*cm))

    def add_keywords(self):
        """添加关键词分析"""
        self.story.append(Paragraph("三、热门关键词", self.heading1_style))

        keywords = self.analysis_result.get('关键词', [])[:15]

        if keywords:
            keyword_data = [['排名', '关键词', '权重']]
            for idx, kw in enumerate(keywords, 1):
                keyword_data.append([
                    str(idx),
                    kw.get('关键词', ''),
                    kw.get('权重', '')
                ])

            table = Table(keyword_data, colWidths=[3*cm, 8*cm, 4*cm])
            table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e74c3c')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, -1), 'Chinese'),
                ('FONTSIZE', (0, 0), (-1, 0), 12),
                ('FONTSIZE', (0, 1), (-1, -1), 10),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#fadbd8')),
                ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#bdc3c7')),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f9f9f9')])
            ]))

            self.story.append(table)
        else:
            self.story.append(Paragraph("暂无关键词数据", self.body_style))

        self.story.append(Spacer(1, 0.5*cm))

    def add_hot_comments(self):
        """添加热门评论"""
        self.story.append(PageBreak())
        self.story.append(Paragraph("四、热门评论TOP10", self.heading1_style))

        hot_comments = self.analysis_result.get('热门评论', [])

        if hot_comments:
            for idx, comment in enumerate(hot_comments, 1):
                # 评论标题
                comment_title = f"<b>#{idx} {comment.get('用户名', '匿名用户')}</b> | 点赞: {comment.get('点赞数', 0)} | {comment.get('发布时间', '')}"
                self.story.append(Paragraph(comment_title, self.heading2_style))

                # 评论内容
                content = comment.get('评论内容', '').replace('\n', '<br/>')
                self.story.append(Paragraph(content, self.body_style))
                self.story.append(Spacer(1, 0.3*cm))
        else:
            self.story.append(Paragraph("暂无热门评论数据", self.body_style))

        self.story.append(Spacer(1, 0.5*cm))

    def add_active_users(self):
        """添加活跃用户分析"""
        self.story.append(Paragraph("五、活跃用户TOP10", self.heading1_style))

        active_users = self.analysis_result.get('活跃用户', [])

        if active_users:
            user_data = [['排名', '用户名', '评论数']]
            for idx, user in enumerate(active_users, 1):
                user_data.append([
                    str(idx),
                    user.get('用户名', ''),
                    f"{user.get('评论数', 0)} 条"
                ])

            table = Table(user_data, colWidths=[3*cm, 8*cm, 4*cm])
            table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#9b59b6')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, -1), 'Chinese'),
                ('FONTSIZE', (0, 0), (-1, 0), 12),
                ('FONTSIZE', (0, 1), (-1, -1), 10),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#ebdef0')),
                ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#bdc3c7')),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f9f9f9')])
            ]))

            self.story.append(table)
        else:
            self.story.append(Paragraph("暂无活跃用户数据", self.body_style))

        self.story.append(Spacer(1, 0.5*cm))

    def add_footer(self):
        """添加报告结尾"""
        self.story.append(PageBreak())
        self.story.append(Paragraph("六、总结与建议", self.heading1_style))

        stats = self.analysis_result.get('基础统计', {})
        sentiment = self.analysis_result.get('情感分析', {})

        # 生成总结
        total_comments = stats.get('总评论数', 0)
        pos_ratio = float(sentiment.get('正面占比', '0%').rstrip('%'))
        neg_ratio = float(sentiment.get('负面占比', '0%').rstrip('%'))

        summary_text = f"""
本次舆情分析共收集了<b>{total_comments}条</b>用户评论，通过多维度分析得出以下结论：
<br/><br/>
<b>1. 整体舆情态势：</b><br/>
正面评论占比{sentiment.get('正面占比', '0%')}，负面评论占比{sentiment.get('负面占比', '0%')}。
{'舆情整体向好，用户满意度较高。' if pos_ratio > 50 else '需要关注负面反馈，及时优化产品和服务。'}
<br/><br/>
<b>2. 用户参与度：</b><br/>
共有{stats.get('用户数', 0)}位用户参与讨论，平均每条评论获得{stats.get('平均点赞数', 0):.1f}个点赞，
{'用户互动积极。' if stats.get('平均点赞数', 0) > 1 else '用户互动有待提升。'}
<br/><br/>
<b>3. 改进建议：</b><br/>
• 持续关注用户反馈，特别是负面评论中提到的问题<br/>
• 加强与活跃用户的互动，培养忠实用户群体<br/>
• 根据热门关键词优化内容策略<br/>
• 定期进行舆情监测，及时发现和处理潜在风险
"""

        self.story.append(Paragraph(summary_text, self.body_style))
        self.story.append(Spacer(1, 1*cm))

        # 报告结束标识
        end_text = "—— 报告结束 ——"
        end_para = Paragraph(end_text, self.title_style)
        self.story.append(end_para)

    def generate(self):
        """生成PDF报告"""
        try:
            print(f"正在生成PDF报告: {self.output_file}")

            # 创建PDF文档
            doc = SimpleDocTemplate(
                self.output_file,
                pagesize=A4,
                rightMargin=2*cm,
                leftMargin=2*cm,
                topMargin=2*cm,
                bottomMargin=2*cm
            )

            # 添加各个部分
            self.add_title()
            self.add_summary()
            self.add_sentiment_analysis()
            self.add_keywords()
            self.add_hot_comments()
            self.add_active_users()
            self.add_footer()

            # 生成PDF
            doc.build(self.story)

            print(f"PDF报告生成成功: {self.output_file}")
            return True

        except Exception as e:
            print(f"生成PDF报告失败: {e}")
            import traceback
            traceback.print_exc()
            return False
