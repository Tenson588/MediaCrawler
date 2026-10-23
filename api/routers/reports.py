"""Local report dashboard: read collected notes and persist reproducible reports."""
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import quote
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from config.db_config import SQLITE_DB_PATH
from api.services.brand_monitor import (
    BRAND_MONITOR_KEYWORDS,
    BRAND_MONITOR_SEEDS,
    brand_monitor_manager,
)
from api.services.dealer_monitor import dealer_monitor_manager
from api.services.rev_monitor import REV_MONITOR_KEYWORDS, rev_monitor_manager
from api.services.report_pdf import generate_report_pdf

router = APIRouter(prefix='/reports', tags=['reports'])
REPORT_DIR = Path(__file__).resolve().parents[2] / 'data' / 'reports'
TZ = ZoneInfo('Asia/Shanghai')
DEAL_WORDS = ('优惠', '折扣', '折起', '满减', '满赠', '闪购', '特卖', '清仓', '到手价', '活动价', '双11', '双十一')
METADATA_PREFIXES = ('生成时间', '发布时间范围', '关键词', '来源关键词', '作者', '发布者', '原文', '搜索词', '排序', '原始结果', '本期高相关', '匹配笔记', '说明')
PRICE_PATTERN = re.compile(
    r'(?:满\s*)?[\d,]+\s*(?:元\s*)?减\s*[\d,]+\s*(?:元)?'
    r'|(?<![\d双])\d+(?:\.\d+)?\s*[—–-]\s*\d+(?:\.\d+)?\s*折(?:起)?'
    r'|(?<![\d双])\d+(?:\.\d+)?\s*折(?:起)?'
    r'|[\d,]+\s*元'
)
DEAL_CONDITION_PATTERNS = (
    ('优惠', re.compile(
        r'折上折|折扣|特价|活动价|到手价|优惠价|好价|包邮|'
        r'\d[\d,.*＊]*\s*元|\d+(?:\.\d+)?\s*件[^。；\n]{0,20}?\d+(?:\.\d+)?\s*折|'
        r'\d+(?:\.\d+)?\s*折|满\s*[\d,]+[^。；\n]{0,12}?(?:减|赠)'
    )),
    ('限制', re.compile(r'不参加|不参与|除外|仅限|限购|特例|指定款|指定品牌')),
    ('福利', re.compile(r'满赠|赠送|送[^。；\n]{0,24}(?:券|礼|袜|皮|帽)|优惠券|代金券|会员.*礼|福利')),
    ('时间', re.compile(
        r'活动时间|有效期|限时|⏰|'
        r'\d{1,2}月\d{1,2}(?:日)?\s*(?:[—–至到-])\s*(?:\d{1,2}月)?\d{1,2}日?|'
        r'\d{1,2}[./-]\d{1,2}\s*(?:[—–至到-])\s*\d{1,2}[./-]\d{1,2}'
    )),
    ('渠道/地点', re.compile(r'线上|线下|同价|小程序|到店|门店|📍|地址')),
)
GENERIC_BRAND_WORDS = {'sale', 'deals', 'shop', 'store'}
BRAND_NOTE_PATTERN = re.compile(
    r'(?i)(?<![a-z0-9])(?:bogner|atomic|aomic|salomon|head|burton|thirtytwo|rossignol)(?![a-z0-9])'
    r'|伯顿|卢西诺|萨洛蒙'
)
BRAND_NOTE_IDS = {str(seed['note_id']) for seed in BRAND_MONITOR_SEEDS}
OVERSEAS_PROMOTION_PATTERN = re.compile(
    r'(?i)🇸🇪|🇨🇦|🇺🇸|🇯🇵|🇰🇷|🇦🇺|🇳🇿|🇬🇧|🇫🇷|🇩🇪|'
    r'卡尔加里|calgary|priddis|canada|sweden|stockholm|'
    r'瑞典(?:现场|当地|特卖|代购)|在瑞典|\d+(?:\.\d+)?\s*(?:kr|sek)\b|克朗|'
    r'(?:香港|澳门|台湾)(?:当地|现场|门店|特卖|活动)|'
    r'(?:美国|日本|韩国|加拿大|欧洲|澳洲|新西兰|英国|法国|德国)(?:当地|现场|特卖会|代购)|'
    r'海外(?:当地|现场|特卖|代购)'
)
PROMOTION_PRIORITY = (
    ('冷山', re.compile(r'冷山', re.I)),
    ('傲天', re.compile(r'傲天', re.I)),
    ('黑桃', re.compile(r'黑桃|spaders', re.I)),
    ('三夫（攀山鼠）', re.compile(r'三夫|攀山鼠|kl[aä]ttermusen', re.I)),
)
REV_NOTE_PATTERN = re.compile(
    r'(?i)(?:(?<![a-z0-9])REV(?![a-z0-9])[^\n]{0,30}(?:头盔|雪盔)'
    r'|(?:头盔|雪盔)[^\n]{0,30}(?<![a-z0-9])REV(?![a-z0-9]))'
)
REV_MODEL_PATTERNS = (
    re.compile(r'(?i)(?<![a-z0-9])REV[\s·/_-]+([A-Z0-9][A-Z0-9-]{1,18})(?![a-z0-9-])'),
    re.compile(r'(?i)(?<![a-z0-9])REV\s*([A-Z][A-Z0-9-]{1,18})\s*(?:头盔|雪盔)'),
    re.compile(r'(?i)(?<![a-z0-9])REV\s*(?:头盔|雪盔)\s*([A-Z0-9][A-Z0-9-]{1,18})(?![a-z0-9-])'),
)
REV_ISSUE_GROUPS = (
    ('结构/撞击保护', re.compile(r'(?<!防)开裂|裂纹|裂开|断裂|破损|碎裂|撞击后?(?:失效|开裂|破损)|摔(?:伤|坏)|受伤|保护失效|缓冲(?:失效|不足|差)|召回')),
    ('佩戴固定', re.compile(r'松动|脱落|晃动|卡扣(?:失效|坏|松|脱落)|扣带(?:失效|断|松|脱落)|系带(?:失效|断|松)|固定(?:不住|失效|松动|问题)|(?<!不)夹头|(?<!不)压头|勒头|尺码(?:不准|偏大|偏小|不合适)')),
    ('视野/舒适', re.compile(r'(?<!防)(?<!别)起雾|视野(?:差|受限|模糊)|通风(?:差|不好|不足|失效)|闷热|异响|风噪(?:大|明显)|护目镜(?:不匹配|顶|漏风)|耳罩(?:松|掉|不适)|不舒服|佩戴不适')),
    ('质量/投诉售后', re.compile(r'质量(?:差|问题|缺陷)|做工(?:差|粗糙)|瑕疵|投诉|差评|避雷|翻车|退货|退款|售后(?:差|问题|不理|拒绝)|维修|不推荐|缺陷|隐患|不安全')),
)
REV_HIGH_RISK_PATTERN = re.compile(r'开裂|裂纹|裂开|断裂|碎裂|撞击后?失效|保护失效|卡扣失效|扣带失效|佩戴中脱落|受伤|事故|召回')
REV_MEDIUM_RISK_PATTERN = re.compile(r'破损|松动|脱落|卡扣|扣带|固定问题|晃动|质量问题|投诉|退货|退款|售后|不安全|隐患')
ReportScope = Literal['dealer', 'brand', 'rev']


def notes():
    path = Path(SQLITE_DB_PATH)
    if not path.exists():
        return []
    try:
        with sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True, timeout=5) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(
                'SELECT note_id,title,desc,nickname,time,last_modify_ts,source_keyword FROM xhs_note ORDER BY time DESC'
            )]
    except sqlite3.Error as exc:
        raise HTTPException(503, '采集数据库暂时不可用，请稍后重试') from exc


def seconds(value):
    value = float(value or 0)
    return value / 1000 if value > 100_000_000_000 else value


def date_text(value):
    return datetime.fromtimestamp(seconds(value), TZ).strftime('%Y-%m-%d %H:%M') if value else '未知'


def timestamp_text(value):
    return datetime.fromtimestamp(seconds(value), TZ).strftime('%Y-%m-%d %H:%M:%S') if value else '未知'


def report_directory(scope: ReportScope) -> Path:
    if scope == 'brand':
        return REPORT_DIR / 'brand'
    if scope == 'rev':
        return REPORT_DIR / 'rev'
    return REPORT_DIR


def report_path(name, scope: ReportScope = 'dealer'):
    if Path(name).name != name or not name.endswith('.md'):
        raise HTTPException(400, '无效的报告名称')
    directory = report_directory(scope)
    path = directory / name
    if path.resolve().parent != directory.resolve() or not path.is_file():
        raise HTTPException(404, '报告不存在')
    return path


def is_brand_note(row: dict) -> bool:
    """Classify brand-monitor records without changing the crawler database schema."""
    if str(row.get('note_id') or '') in BRAND_NOTE_IDS:
        return True
    if (row.get('source_keyword') or '').casefold() in {
        keyword.casefold() for keyword in BRAND_MONITOR_KEYWORDS
    }:
        return True
    text = ' '.join(str(row.get(key) or '') for key in ('title', 'desc', 'source_keyword'))
    return bool(BRAND_NOTE_PATTERN.search(text))


def is_rev_note(row: dict) -> bool:
    if (row.get('source_keyword') or '').casefold() in {
        keyword.casefold() for keyword in REV_MONITOR_KEYWORDS
    }:
        return True
    text = ' '.join(str(row.get(key) or '') for key in ('title', 'desc', 'source_keyword'))
    return bool(REV_NOTE_PATTERN.search(text))


def scoped_notes(rows: list[dict], scope: ReportScope) -> list[dict]:
    if scope == 'rev':
        return [row for row in rows if is_rev_note(row)]
    if scope == 'brand':
        return [row for row in rows if is_brand_note(row) and not is_rev_note(row)]
    return [row for row in rows if not is_brand_note(row) and not is_rev_note(row)]


def rev_issue_analysis(row: dict) -> dict:
    text = ' '.join(str(row.get(key) or '') for key in ('title', 'desc'))
    groups = [label for label, pattern in REV_ISSUE_GROUPS if pattern.search(text)]
    models: list[str] = []
    for pattern in REV_MODEL_PATTERNS:
        for match in pattern.finditer(text):
            model = match.group(1).upper()
            if model not in {'HELMET', 'SKI', 'SNOW', '头盔', '雪盔'} and model not in models:
                models.append(model)
    if REV_HIGH_RISK_PATTERN.search(text):
        risk = '高关注'
    elif REV_MEDIUM_RISK_PATTERN.search(text):
        risk = '中关注'
    elif groups:
        risk = '一般关注'
    else:
        risk = '未发现问题词'
    return {
        'matched': bool(groups),
        'risk': risk,
        'risk_rank': {'高关注': 0, '中关注': 1, '一般关注': 2, '未发现问题词': 3}[risk],
        'groups': groups,
        'models': models or ['型号未明确'],
    }


def build_rev_summary(candidates: list[dict]) -> str:
    if not candidates:
        return '风险概览：当前范围内未提取到明确的 REV 头盔负面、投诉或使用问题线索。\n核实提示：未发现线索不代表不存在风险，仍应结合产品认证、召回公告和实物检测判断。'
    analyses = [(row, rev_issue_analysis(row)) for row in candidates]
    high = sum(analysis['risk'] == '高关注' for _, analysis in analyses)
    medium = sum(analysis['risk'] == '中关注' for _, analysis in analyses)
    model_counts: dict[str, int] = {}
    for _, analysis in analyses:
        for model in analysis['models']:
            model_counts[model] = model_counts.get(model, 0) + 1
    model_text = '、'.join(
        f'{model}（{count}条）' for model, count in
        sorted(model_counts.items(), key=lambda item: (-item[1], item[0]))[:6]
    )
    focus_row, focus = analyses[0]
    focus_url = f'https://www.xiaohongshu.com/explore/{focus_row.get("note_id") or ""}'
    return '\n'.join([
        f'风险概览：共发现 {len(candidates)} 条问题候选，其中高关注 {high} 条、中关注 {medium} 条。',
        f'重点线索：“{focus_row.get("title") or "无标题"}”涉及{"、".join(focus["groups"])}；型号：{"、".join(focus["models"])}；原文：{focus_url}。',
        f'型号分布：{model_text}。',
        '核实提示：用户帖子仅作为风险线索，不能单独证明产品存在安全隐患；需要结合认证、召回公告、事故材料和实物检测核实。',
    ])


def build_rev_report_summary(content: str) -> str:
    """Rebuild a stable REV summary from a saved report without reading the live database."""
    count_match = re.search(
        r'共发现 \*\*(\d+) 条\*\*负面、投诉或使用问题候选线索，其中 \*\*(\d+) 条\*\*为高关注',
        content,
    )
    total = int(count_match.group(1)) if count_match else 0
    high = int(count_match.group(2)) if count_match else 0
    if not total:
        return build_rev_summary([])
    first_section = re.search(r'^###\s+\d+[.、]\s*(.+?)(?=^###\s|^##\s|\Z)', content, re.M | re.S)
    focus_line = ''
    if first_section:
        block = first_section.group(0)
        title = first_section.group(1).splitlines()[0].strip()
        model = re.search(r'^- 型号：(.+)$', block, re.M)
        groups = re.search(r'^- 问题类型：(.+)$', block, re.M)
        url = re.search(r'https?://\S+', block)
        focus_line = (
            f'重点线索：“{title}”涉及{groups.group(1) if groups else "待人工分类"}；'
            f'型号：{model.group(1) if model else "型号未明确"}；'
            f'原文：{url.group(0) if url else "未提供"}。'
        )
    model_section = re.search(r'^## 按型号汇总\s*(.+?)(?=^##\s|\Z)', content, re.M | re.S)
    model_lines = re.findall(r'^- \*\*(.+?)\*\*：([^\n]+)', model_section.group(1), re.M) if model_section else []
    model_text = '、'.join(f'{model}（{detail.split("；", 1)[0]}）' for model, detail in model_lines[:6])
    parts = [f'风险概览：共发现 {total} 条问题候选，其中高关注 {high} 条。']
    if focus_line:
        parts.append(focus_line)
    if model_text:
        parts.append(f'型号分布：{model_text}。')
    parts.append('核实提示：用户帖子仅作为风险线索，不能单独证明产品存在安全隐患；需要结合认证、召回公告、事故材料和实物检测核实。')
    return '\n'.join(parts)


def promotion_text(row: dict) -> str:
    return ' '.join(str(row.get(key) or '') for key in ('title', 'desc', 'source_keyword', 'nickname'))


def is_mainland_promotion(row: dict) -> bool:
    """Keep unknown-location records, but reject candidates with explicit overseas signals."""
    return not OVERSEAS_PROMOTION_PATTERN.search(promotion_text(row))


def promotion_sort_key(row: dict) -> tuple[int, float, str]:
    text = promotion_text(row)
    priority = next(
        (index for index, (_, pattern) in enumerate(PROMOTION_PRIORITY) if pattern.search(text)),
        len(PROMOTION_PRIORITY),
    )
    return priority, -seconds(row.get('time')), str(row.get('title') or '').casefold()


def _plain_text(value: str) -> str:
    value = re.sub(r'\[([^]]+)]\([^)]*\)', r'\1', value)
    value = re.sub(r'https?://\S+', '', value)
    value = re.sub(r'^[\s>*#-]+', '', value)
    value = value.replace('**', '').replace('`', '').strip()
    return re.sub(r'\s+', ' ', value).strip('；。 ')


def _sections(content: str) -> list[dict]:
    sections: list[dict] = []
    current = {'title': '报告概览', 'lines': []}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if re.match(r'^#{2,3}\s+', line):
            if current['lines']:
                sections.append(current)
            current = {'title': re.sub(r'^#{2,3}\s+(?:\d+[.、]\s*)?', '', line), 'lines': []}
        elif line:
            current['lines'].append(line)
    if current['lines']:
        sections.append(current)
    return [section for section in sections if section['title'] != '重点摘要']


def _discount_score(text: str) -> tuple[float, str] | None:
    """Return a comparable saving score and the matching phrase."""
    candidates: list[tuple[float, str]] = []
    for match in re.finditer(r'(?<![\d双])(\d+(?:\.\d+)?)\s*[—–-]?\s*(\d+(?:\.\d+)?)?\s*折(?:起)?', text):
        first = float(match.group(1))
        second = float(match.group(2)) if match.group(2) else first
        rate = min(first, second)
        if rate > 10:
            rate /= 10
        if 0 < rate <= 10:
            candidates.append(((10 - rate) / 10, match.group(0)))
    for match in re.finditer(r'满\s*([\d,]+)\s*(?:元)?\s*减\s*([\d,]+)', text):
        threshold = float(match.group(1).replace(',', ''))
        reduction = float(match.group(2).replace(',', ''))
        if threshold > 0 and reduction <= threshold:
            candidates.append((reduction / threshold, match.group(0)))
    price_pair = re.search(
        r'(?:原价|吊牌价)\s*(?:约)?\s*([\d,]+)\s*元[^。；\n]{0,35}?'
        r'(?:活动价|奥莱价|到手价|现价|组合价)\s*(?:为)?\s*([\d,]+)',
        text,
    )
    if price_pair:
        original = float(price_pair.group(1).replace(',', ''))
        current = float(price_pair.group(2).replace(',', ''))
        if original > 0 and current <= original:
            candidates.append(((original - current) / original, price_pair.group(0)))
    return max(candidates, default=None, key=lambda item: item[0])


def _published_at(text: str, default_year: int) -> tuple[datetime, str] | None:
    match = re.search(r'发布时间[：:]\s*(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})(?:日)?(?:\s+(\d{1,2}):(\d{2}))?', text)
    if match:
        published = datetime(
            int(match.group(1)), int(match.group(2)), int(match.group(3)),
            int(match.group(4) or 0), int(match.group(5) or 0), tzinfo=TZ,
        )
        return published, match.group(0)
    match = re.search(r'(\d{1,2})\s*月\s*(\d{1,2})\s*日[^\n]{0,16}?(?:新帖|发布|出现)', text)
    if match:
        line = next((line for line in text.splitlines() if match.group(0) in line), match.group(0))
        return datetime(default_year, int(match.group(1)), int(match.group(2)), tzinfo=TZ), line
    return None


def build_report_summary(content: str) -> str:
    """Create a short deal-first summary for both old and newly generated reports."""
    sections = _sections(content)
    year_match = re.search(r'20\d{2}', content)
    default_year = int(year_match.group(0)) if year_match else datetime.now(TZ).year
    strongest: tuple[float, str, str, str, str] | None = None
    latest: tuple[datetime, str, str, str, str] | None = None
    fallback: tuple[str, str, str, str] | None = None

    for section in sections:
        title = _plain_text(section['title'])
        section_text = '\n'.join(section['lines'])
        author_match = re.search(r'(?:作者|发布者)[：:]\s*([^；;\n]+)', section_text)
        url_match = re.search(r'https?://[^\s)]+', section_text)
        author = re.sub(r'\s+', ' ', author_match.group(1)).strip() if author_match else '未知'
        source_url = url_match.group(0) if url_match else '未提供'
        if fallback is None and title != '报告概览' and any(word in title for word in DEAL_WORDS):
            fallback = (title, title, author, source_url)
        for line in section['lines']:
            plain_line = _plain_text(line)
            if not plain_line:
                continue
            if any(plain_line.startswith(prefix + marker) for prefix in METADATA_PREFIXES for marker in ('：', ':')):
                continue
            scored = _discount_score(plain_line)
            if scored and (strongest is None or scored[0] > strongest[0]):
                strongest = (scored[0], title, plain_line, author, source_url)
            if fallback is None and any(word in plain_line for word in DEAL_WORDS):
                fallback = (title, plain_line, author, source_url)
        published_result = _published_at(section_text, default_year)
        if published_result and (latest is None or published_result[0] > latest[0]):
            published, matched_line = published_result
            detail = _plain_text(matched_line)
            if detail.startswith('发布时间'):
                detail = next((
                    _plain_text(line) for line in section['lines']
                    if any(word in line for word in DEAL_WORDS)
                    and not any(_plain_text(line).startswith(prefix + marker)
                                for prefix in METADATA_PREFIXES for marker in ('：', ':'))
                ), '')
            latest = (published, title, detail, author, source_url)

    parts: list[str] = []
    deal = strongest or ((0, *fallback) if fallback else None)
    if deal:
        _, title, detail, author, source_url = deal
        prefix = '最大优惠' if strongest else '优惠焦点'
        if detail == title:
            description = f'“{title}”被列为促销候选线索'
        else:
            description = f'“{title}”中提到{detail[:100]}'
        parts.append(f'{prefix}：{description}（发布者：{author}；原文：{source_url}）。')
    if latest:
        published, title, detail, author, source_url = latest
        movement = f'最新动向：{published:%Y-%m-%d %H:%M} 发布的“{title}”'
        if detail and (not deal or detail != deal[2]):
            movement += f'提到{detail[:70]}'
        parts.append(f'{movement}（发布者：{author}；原文：{source_url}）。')
    if not parts:
        return '当前报告没有提取到明确的优惠数字或最新活动动向，建议查看正文并核实活动有效期、库存和购买渠道。'
    return '\n'.join(parts)


def without_embedded_summary(content: str) -> str:
    """Remove legacy inline summaries because the report viewer renders one separately."""
    lines = content.splitlines()
    cleaned: list[str] = []
    skipping = False
    for line in lines:
        if line.strip() == '## 重点摘要':
            skipping = True
            continue
        if skipping and line.strip().startswith('## '):
            skipping = False
        if not skipping:
            cleaned.append(line)
    return '\n'.join(cleaned).strip() + '\n'


def summary_markdown(summary: str) -> str:
    """Format a plain-text summary as readable Markdown with emphasized prices."""
    formatted = []
    for line in summary.splitlines():
        match = re.match(r'^([^：:]+)[：:]\s*(.*)$', line)
        if not match:
            formatted.append(f'- {PRICE_PATTERN.sub(lambda item: f"**{item.group(0)}**", line)}')
            continue
        label, detail = match.groups()
        detail = PRICE_PATTERN.sub(lambda item: f'**{item.group(0)}**', detail)
        formatted.append(f'- **{label}：** {detail}')
    return '\n'.join(formatted)


def extract_deal_conditions(text: str) -> list[tuple[str, str]]:
    """Extract readable prices, eligibility limits, gifts, dates and channels."""
    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_line in text.splitlines():
        for fragment in re.split(r'[；;。]\s*', raw_line):
            line = re.sub(r'\s+', ' ', fragment).strip(' \t-*▫️✅⚠️')
            line = re.sub(r'\s*#[^#]+(?:\[话题])?#.*$', '', line).strip()
            if not line:
                continue
            for label, pattern in DEAL_CONDITION_PATTERNS:
                if not pattern.search(line):
                    continue
                item = (label, line[:180])
                if item not in seen:
                    result.append(item)
                    seen.add(item)
                break
    return result[:10]


def _brand_tokens(row: dict) -> set[str]:
    text = f'{row.get("title") or ""} {row.get("desc") or ""}'
    return {
        token.casefold() for token in re.findall(r'[A-Za-z][A-Za-z0-9-]{3,}', text)
        if token.casefold() not in GENERIC_BRAND_WORDS
    }


def deal_condition_context(row: dict, all_rows: list[dict]) -> tuple[list[tuple[str, str]], dict | None]:
    """Prefer the note itself, then a nearby same-keyword note for the same brand."""
    direct = extract_deal_conditions(row.get('desc') or '')
    if direct:
        return direct, None
    tokens = _brand_tokens(row)
    if not tokens:
        return [], None
    published = seconds(row.get('time'))
    related: list[tuple[int, float, dict, list[tuple[str, str]]]] = []
    for other in all_rows:
        if other.get('note_id') == row.get('note_id') or other.get('source_keyword') != row.get('source_keyword'):
            continue
        if not tokens.intersection(_brand_tokens(other)):
            continue
        distance = abs(published - seconds(other.get('time')))
        if distance > 30 * 86400:
            continue
        conditions = extract_deal_conditions(other.get('desc') or '')
        if conditions and any(label in {'优惠', '限制', '福利'} for label, _ in conditions):
            related.append((len(conditions), -distance, other, conditions))
    if not related:
        return [], None
    _, _, source, conditions = max(related, key=lambda item: (item[0], item[1]))
    return conditions, source


@router.get('/overview')
def overview(scope: ReportScope = 'dealer'):
    rows = scoped_notes(notes(), scope)
    last_collected = max((seconds(row['last_modify_ts']) for row in rows), default=0)
    keywords = sorted({row['source_keyword'] for row in rows if row['source_keyword']})
    if not keywords:
        if scope == 'rev':
            keywords = list(REV_MONITOR_KEYWORDS)
        elif scope == 'brand':
            keywords = list(BRAND_MONITOR_KEYWORDS)
        else:
            from config.base_config import KEYWORDS
            keywords = [word.strip() for word in KEYWORDS.split(',') if word.strip()]
    rendered_notes = []
    issue_count = 0
    for row in rows:
        item = {**row, 'published': date_text(row['time'])}
        if scope == 'rev':
            analysis = rev_issue_analysis(row)
            item['rev_issue'] = analysis
            issue_count += int(analysis['matched'])
        rendered_notes.append(item)
    return {
        'scope': scope,
        'keywords': keywords,
        'total': len(rows),
        'issue_count': issue_count if scope == 'rev' else None,
        'last_collected': date_text(last_collected),
        'database_timestamp': timestamp_text(last_collected),
        'notes': rendered_notes,
    }


@router.get('')
def history(q: str = Query('', max_length=200), scope: ReportScope = 'dealer'):
    result = []
    for path in report_directory(scope).glob('*.md'):
        if path.is_symlink():
            continue
        content = path.read_text(encoding='utf-8')
        title = content.splitlines()[0].lstrip('# ') if content else path.stem
        if q.casefold() not in (title + content).casefold():
            continue
        result.append({'name': path.name, 'title': title, 'created_at': path.stat().st_mtime,
                       'size': path.stat().st_size})
    return {'scope': scope, 'reports': sorted(result, key=lambda item: item['created_at'], reverse=True)}


class GenerateRequest(BaseModel):
    keywords: str = Field('', max_length=2000)
    days: int = Field(7, ge=1, le=365)
    scope: ReportScope = 'dealer'


class DealerMonitorRequest(BaseModel):
    keywords: str = Field('', max_length=2000)


def generate_rev_report(request: GenerateRequest, now: datetime, rows: list[dict]) -> dict:
    candidates = sorted(
        (row for row in rows if rev_issue_analysis(row)['matched']),
        key=lambda row: (rev_issue_analysis(row)['risk_rank'], -seconds(row.get('time'))),
    )
    high_attention = sum(rev_issue_analysis(row)['risk'] == '高关注' for row in candidates)
    model_summary: dict[str, dict[str, object]] = {}
    for row in candidates:
        analysis = rev_issue_analysis(row)
        for model in analysis['models']:
            item = model_summary.setdefault(model, {'count': 0, 'groups': set(), 'high': 0})
            item['count'] += 1
            item['groups'].update(analysis['groups'])
            item['high'] += int(analysis['risk'] == '高关注')

    lines = [
        f'# REV头盔风险监控报告 · {now:%Y-%m-%d %H:%M}', '',
        '## 问题候选线索', '',
        f'共发现 **{len(candidates)} 条**负面、投诉或使用问题候选线索，其中 **{high_attention} 条**为高关注。',
        '关注等级用于整理线索，不代表已经确认产品存在安全隐患。', '',
    ]
    if not candidates:
        lines.extend(['当前范围内没有提取到明确问题词，可扩大时间范围后重试。', ''])
    for index, row in enumerate(candidates, start=1):
        analysis = rev_issue_analysis(row)
        note_id = str(row.get('note_id') or '')
        lines.extend([
            f'### {index}. {row.get("title") or "无标题"}', '',
            f'- 作者：{row.get("nickname") or "未知"}；发布时间：{date_text(row.get("time"))}',
            f'- 型号：{"、".join(analysis["models"])}',
            f'- 关注等级：**{analysis["risk"]}**',
            f'- 问题类型：{"、".join(analysis["groups"])}',
            f'- 来源关键词：{row.get("source_keyword") or "未知"}',
            f'- 原文：https://www.xiaohongshu.com/explore/{note_id}', '',
            row.get('desc') or '正文为空', '',
        ])

    lines.extend(['## 按型号汇总', ''])
    if model_summary:
        for model, item in sorted(
            model_summary.items(),
            key=lambda pair: (-int(pair[1]['high']), -int(pair[1]['count']), pair[0]),
        ):
            groups = '、'.join(sorted(item['groups']))
            lines.append(
                f'- **{model}**：{item["count"]} 条问题候选；高关注 {item["high"]} 条；涉及 {groups}'
            )
    else:
        lines.append('- 暂无可汇总的问题型号。')

    lines.extend(['', '## 全部匹配笔记', ''])
    lines.extend(
        f'- {date_text(row["time"])} | {row.get("title") or "无标题"} | '
        f'https://www.xiaohongshu.com/explore/{row.get("note_id") or ""}'
        for row in rows
    )
    lines.extend([
        '', '## 备注', '',
        f'- 生成时间：{now:%Y-%m-%d %H:%M:%S}（Asia/Shanghai）',
        f'- 发布时间范围：最近 {request.days} 天',
        f'- 关键词：{request.keywords or "全部 REV 专项记录"}',
        f'- 匹配笔记：{len(rows)} 条；问题候选：{len(candidates)} 条；高关注：{high_attention} 条',
        '- 排序：高关注 → 中关注 → 一般关注；同级按发布时间倒序', '',
        '> 本报告基于本地已采集的用户内容生成，不会发起新的采集。负面评价、投诉和使用问题仅作为风险线索，不能单独证明某一型号存在安全隐患；请结合官方认证、召回公告、事故材料和实物检测核实。',
    ])
    content = '\n'.join(lines) + '\n'
    summary = build_rev_summary(candidates)
    directory = report_directory('rev')
    directory.mkdir(parents=True, exist_ok=True)
    name = f'{now:%Y%m%d-%H%M%S}-{uuid4().hex[:8]}.md'
    temporary = directory / (name + '.tmp')
    temporary.write_text(content, encoding='utf-8')
    temporary.replace(directory / name)
    return {
        'name': name,
        'scope': 'rev',
        'content': content,
        'summary': summary,
        'matched': len(rows),
        'candidates': len(candidates),
        'high_attention': high_attention,
    }


@router.post('/generate')
def generate(request: GenerateRequest):
    now = datetime.now(TZ)
    cutoff = (now - timedelta(days=request.days)).timestamp()
    terms = [term.strip() for term in re.split('[,，\n]', request.keywords) if term.strip()]
    all_rows = scoped_notes(notes(), request.scope)
    rows = [row for row in all_rows if cutoff <= seconds(row['time']) <= now.timestamp()
            and (not terms or any(term.casefold() in ' '.join(str(row.get(key) or '') for key in
                ('title', 'desc', 'source_keyword')).casefold() for term in terms))]
    if request.scope == 'rev':
        return generate_rev_report(request, now, rows)
    signals = ('特卖', '清仓', '折扣', '满减', '闪购', '优惠', '双11', '双十一', '内购', '奥莱')
    raw_candidates = [
        row for row in rows
        if any(term in (row['title'] or '') + (row['desc'] or '') for term in signals)
    ]
    candidates = sorted(
        (row for row in raw_candidates if is_mainland_promotion(row)),
        key=promotion_sort_key,
    )
    filtered_overseas = len(raw_candidates) - len(candidates)
    scope_title = '品牌' if request.scope == 'brand' else '经销商'
    lines = [f'# 小红书{scope_title}监控报告 · {now:%Y-%m-%d %H:%M}', '', '## 促销候选线索', '',
             f'共发现 **{len(candidates)} 条**中国大陆促销候选线索。']
    if filtered_overseas:
        lines.append(f'已过滤 **{filtered_overseas} 条**明确属于非中国大陆的候选信息。')
    lines.append('')
    if not candidates:
        lines.append('当前范围内没有发现含促销词的笔记。可扩大时间范围或调整关键词。')
    for index, row in enumerate(candidates, start=1):
        note_id = str(row['note_id'] or '')
        lines.extend([f'### {index}. {row["title"] or "无标题"}', '',
                      f'- 作者：{row["nickname"] or "未知"}；发布时间：{date_text(row["time"])}',
                      f'- 来源关键词：{row["source_keyword"] or "未知"}',
                      f'- 原文：https://www.xiaohongshu.com/explore/{note_id}'])
        conditions, related_source = deal_condition_context(row, all_rows)
        if conditions:
            lines.append('- 价格与活动条件：')
            lines.extend(f'  - **{label}**：{detail}' for label, detail in conditions)
            if related_source:
                related_id = str(related_source.get('note_id') or '')
                lines.append(
                    f'  - **关联来源**：{date_text(related_source.get("time"))} · '
                    f'{related_source.get("title") or "无标题"} · '
                    f'https://www.xiaohongshu.com/explore/{related_id}'
                )
        lines.extend(['', row['desc'] or '正文为空', ''])
    lines.extend(['## 全部匹配笔记', ''])
    lines.extend(f'- {date_text(row["time"])} | {row["title"] or "无标题"} | https://www.xiaohongshu.com/explore/{row["note_id"]}' for row in rows)
    lines.extend([
        '', '## 备注', '',
        f'- 生成时间：{now:%Y-%m-%d %H:%M:%S}（Asia/Shanghai）',
        f'- 发布时间范围：最近 {request.days} 天',
        f'- 关键词：{request.keywords or "全部"}',
        f'- 匹配笔记：{len(rows)} 条；中国大陆促销候选：{len(candidates)} 条；'
        f'已过滤非中国大陆候选：{filtered_overseas} 条',
        '- 排序：冷山 → 傲天 → 黑桃/SPADERS → 三夫/攀山鼠 → 其他；同级按发布时间倒序',
        '',
        '> 本报告基于本地已采集数据生成，不会发起新的采集。促销词命中仅代表候选线索，活动有效期、价格和线上购买渠道需要核实。',
    ])
    content = '\n'.join(lines) + '\n'
    summary = build_report_summary(content)
    directory = report_directory(request.scope)
    directory.mkdir(parents=True, exist_ok=True)
    name = f'{now:%Y%m%d-%H%M%S}-{uuid4().hex[:8]}.md'
    temporary = directory / (name + '.tmp')
    temporary.write_text(content, encoding='utf-8')
    temporary.replace(directory / name)
    return {
        'name': name,
        'scope': request.scope,
        'content': content,
        'summary': summary,
        'matched': len(rows),
        'candidates': len(candidates),
        'filtered_overseas': filtered_overseas,
    }


@router.get('/dealer-monitor')
def dealer_monitor_status():
    return dealer_monitor_manager.snapshot()


@router.post('/dealer-monitor/start')
async def start_dealer_monitor(request: DealerMonitorRequest):
    terms = [term.strip() for term in re.split('[,，\n]', request.keywords) if term.strip()]
    try:
        started = await dealer_monitor_manager.start(terms)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not started:
        raise HTTPException(409, '已有采集任务正在运行，请等待完成后重试')
    return dealer_monitor_manager.snapshot()


@router.get('/rev-monitor')
def rev_monitor_status():
    return rev_monitor_manager.snapshot()


@router.post('/rev-monitor/start')
async def start_rev_monitor():
    if not await rev_monitor_manager.start():
        raise HTTPException(409, '已有采集任务正在运行，请等待完成后重试')
    return rev_monitor_manager.snapshot()


@router.get('/brand-monitor')
def brand_monitor_status():
    return brand_monitor_manager.snapshot()


@router.post('/brand-monitor/start')
async def start_brand_monitor():
    if not await brand_monitor_manager.start():
        raise HTTPException(409, '已有采集任务正在运行，请等待完成后重试')
    return brand_monitor_manager.snapshot()


@router.post('/brand-monitor/stop')
async def stop_brand_monitor():
    if not await brand_monitor_manager.stop():
        raise HTTPException(400, '当前没有运行中的品牌监控任务')
    return brand_monitor_manager.snapshot()


@router.get('/{name}/download')
def download(name: str, scope: ReportScope = 'dealer'):
    content = without_embedded_summary(report_path(name, scope).read_text(encoding='utf-8'))
    return Response(
        content,
        media_type='text/markdown; charset=utf-8',
        headers={
            'Content-Disposition': (
                f'attachment; filename="report.md"; filename*=UTF-8\'\'{quote(name)}'
            )
        },
    )


@router.get('/{name}/pdf')
def download_pdf(name: str, scope: ReportScope = 'dealer'):
    path = report_path(name, scope)
    content = path.read_text(encoding='utf-8')
    filename = f'{path.stem}.pdf'
    summary = build_rev_report_summary(content) if scope == 'rev' else build_report_summary(content)
    return Response(
        generate_report_pdf(content, summary),
        media_type='application/pdf',
        headers={
            'Content-Disposition': (
                f'attachment; filename="report.pdf"; filename*=UTF-8\'\'{quote(filename)}'
            )
        },
    )


@router.get('/{name}')
def detail(name: str, scope: ReportScope = 'dealer'):
    source = report_path(name, scope).read_text(encoding='utf-8')
    if scope == 'rev':
        summary = build_rev_report_summary(source)
    else:
        summary = build_report_summary(source)
    return {
        'name': name,
        'scope': scope,
        'content': without_embedded_summary(source),
        'summary': summary,
    }


@router.delete('/{name}')
def delete_report(name: str, scope: ReportScope = 'dealer'):
    report_path(name, scope).unlink()
    return {'name': name, 'scope': scope, 'deleted': True}
