"""Render report Markdown as a polished, Chinese-capable PDF document."""

from __future__ import annotations

import html
import re
from io import BytesIO
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FONT_PATH = PROJECT_ROOT / "docs" / "STZHONGS.TTF"
FONT_NAME = "MediaCrawlerChinese"
PRICE_PATTERN = re.compile(
    r'(?:满\s*)?[\d,]+\s*(?:元\s*)?减\s*[\d,]+\s*(?:元)?'
    r'|(?<![\d双])\d+(?:\.\d+)?\s*[—–-]\s*\d+(?:\.\d+)?\s*折(?:起)?'
    r'|(?<![\d双])\d+(?:\.\d+)?\s*折(?:起)?'
    r'|[\d,]+\s*元'
)


def _register_font() -> None:
    if FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(FONT_NAME, str(FONT_PATH)))


def _inline_markup(text: str, *, highlight_prices: bool = False) -> str:
    links: list[tuple[str, str]] = []

    def remember_link(match: re.Match) -> str:
        label, url = match.group(1), match.group(2)
        links.append((f'{label} · {url}', url))
        return f"@@LINK{len(links) - 1}@@"

    def remember_raw_link(match: re.Match) -> str:
        raw = match.group(0)
        url = raw.rstrip('）。；，、.!！？?')
        links.append((url, url))
        return f"@@LINK{len(links) - 1}@@{raw[len(url):]}"

    text = text.replace('[话题]', '').replace('#', '')
    text = re.sub(r'\[([^]]+)]\((https?://[^)]+)\)', remember_link, text)
    text = re.sub(r'https?://\S+', remember_raw_link, text)
    text = html.escape(text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'`([^`]+)`', r'<font color="#176f65">\1</font>', text)
    if highlight_prices:
        text = PRICE_PATTERN.sub(
            lambda match: f'<font color="#9a4d00"><b>{match.group(0)}</b></font>',
            text,
        )
    for index, (label, url) in enumerate(links):
        link = (
            f'<link href="{html.escape(url, quote=True)}" color="#087d77">'
            f'{html.escape(label)}</link>'
        )
        text = text.replace(f'@@LINK{index}@@', link)
    return text


def _styles() -> dict[str, ParagraphStyle]:
    sample = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "ReportTitle", parent=sample["Title"], fontName=FONT_NAME,
            fontSize=22, leading=30, textColor=colors.HexColor("#16323B"),
            alignment=TA_CENTER, spaceAfter=8 * mm,
        ),
        "h2": ParagraphStyle(
            "ReportH2", parent=sample["Heading2"], fontName=FONT_NAME,
            fontSize=15, leading=22, textColor=colors.HexColor("#087D77"),
            spaceBefore=5 * mm, spaceAfter=2.5 * mm, keepWithNext=True,
        ),
        "h3": ParagraphStyle(
            "ReportH3", parent=sample["Heading3"], fontName=FONT_NAME,
            fontSize=12.5, leading=19, textColor=colors.HexColor("#244851"),
            spaceBefore=3.5 * mm, spaceAfter=1.5 * mm, keepWithNext=True,
        ),
        "body": ParagraphStyle(
            "ReportBody", parent=sample["BodyText"], fontName=FONT_NAME,
            fontSize=10.2, leading=17, textColor=colors.HexColor("#31464F"),
            spaceAfter=1.8 * mm, wordWrap="CJK",
        ),
        "bullet": ParagraphStyle(
            "ReportBullet", parent=sample["BodyText"], fontName=FONT_NAME,
            fontSize=10, leading=16, leftIndent=5 * mm, firstLineIndent=-3.5 * mm,
            textColor=colors.HexColor("#31464F"), spaceAfter=1.5 * mm, wordWrap="CJK",
        ),
        "quote": ParagraphStyle(
            "ReportQuote", parent=sample["BodyText"], fontName=FONT_NAME,
            fontSize=9.5, leading=15, leftIndent=5 * mm, rightIndent=5 * mm,
            textColor=colors.HexColor("#60777D"), borderColor=colors.HexColor("#B9DDD5"),
            borderWidth=0, borderPadding=6, backColor=colors.HexColor("#F4FAF8"),
            spaceAfter=3 * mm, wordWrap="CJK",
        ),
        "summary_label": ParagraphStyle(
            "SummaryLabel", parent=sample["BodyText"], fontName=FONT_NAME,
            fontSize=10.5, leading=17, textColor=colors.HexColor("#087D77"),
        ),
        "summary_body": ParagraphStyle(
            "SummaryBody", parent=sample["BodyText"], fontName=FONT_NAME,
            fontSize=10.5, leading=17, textColor=colors.HexColor("#29484E"), wordWrap="CJK",
        ),
        "table": ParagraphStyle(
            "ReportTable", parent=sample["BodyText"], fontName=FONT_NAME,
            fontSize=8.4, leading=13, textColor=colors.HexColor("#31464F"), wordWrap="CJK",
        ),
    }


def _summary_table(summary: str, styles: dict[str, ParagraphStyle]) -> Table:
    rows = []
    for line in summary.splitlines():
        match = re.match(r'^([^：:]+)[：:]\s*(.*)$', line)
        if match:
            label, detail = match.groups()
        else:
            label, detail = "摘要", line
        rows.append([
            Paragraph(f'<b>{html.escape(label)}</b>', styles["summary_label"]),
            Paragraph(_inline_markup(detail, highlight_prices=True), styles["summary_body"]),
        ])
    table = Table(rows, colWidths=[27 * mm, 142 * mm], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EDF8F5")),
        ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#B9DDD5")),
        ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D6EBE6")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return table


def _markdown_table(lines: list[str], styles: dict[str, ParagraphStyle], width: float) -> Table:
    rows = []
    for line in lines:
        cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
        if all(re.fullmatch(r':?-{3,}:?', cell) for cell in cells):
            continue
        rows.append([Paragraph(_inline_markup(cell), styles["table"]) for cell in cells])
    columns = max((len(row) for row in rows), default=1)
    table = Table(rows, colWidths=[width / columns] * columns, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E7F3F1")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#16323B")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#C9DADC")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


def generate_report_pdf(content: str, summary: str) -> bytes:
    _register_font()
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm,
        topMargin=20 * mm, bottomMargin=20 * mm,
        title="MediaCrawler 监控报告", author="MediaCrawler",
    )
    styles = _styles()
    lines = content.splitlines()
    title = next((line[2:].strip() for line in lines if line.startswith('# ')), '监控报告')
    story = [Paragraph(_inline_markup(title), styles["title"])]
    story.append(KeepTogether([
        Paragraph("重点摘要", styles["h2"]),
        _summary_table(summary, styles),
        Spacer(1, 3 * mm),
    ]))

    index = 0
    current_section = ''
    promotion_candidate_seen = False
    while index < len(lines):
        line = lines[index].strip()
        if line == '## 重点摘要':
            index += 1
            while index < len(lines) and not lines[index].strip().startswith('## '):
                index += 1
            continue
        if not line or line.startswith('# '):
            index += 1
            continue
        if line.startswith('## '):
            current_section = line[3:].strip()
            story.append(Paragraph(_inline_markup(current_section), styles["h2"]))
        elif line.startswith('### '):
            heading = Paragraph(_inline_markup(line[4:]), styles["h3"])
            if current_section in {'促销候选线索', '问题候选线索'} and promotion_candidate_seen:
                story.append(KeepTogether([
                    HRFlowable(
                        width='100%', thickness=0.65,
                        color=colors.HexColor('#D6E3E5'),
                        spaceBefore=3.5 * mm, spaceAfter=1.5 * mm,
                    ),
                    heading,
                ]))
            else:
                story.append(heading)
            if current_section in {'促销候选线索', '问题候选线索'}:
                promotion_candidate_seen = True
        elif line.startswith('|'):
            table_lines = []
            while index < len(lines) and lines[index].strip().startswith('|'):
                table_lines.append(lines[index].strip())
                index += 1
            story.append(_markdown_table(table_lines, styles, document.width))
            story.append(Spacer(1, 3 * mm))
            continue
        elif line.startswith('>'):
            story.append(Paragraph(_inline_markup(line.lstrip('> ')), styles["quote"]))
        elif re.match(r'^[-*]\s+', line):
            story.append(Paragraph('• ' + _inline_markup(re.sub(r'^[-*]\s+', '', line)), styles["bullet"]))
        else:
            story.append(Paragraph(_inline_markup(line), styles["body"]))
        index += 1

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(FONT_NAME, 8)
        canvas.setFillColor(colors.HexColor("#71868B"))
        canvas.drawString(18 * mm, 10 * mm, "MediaCrawler 本地监控报告")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"第 {doc.page} 页")
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()
