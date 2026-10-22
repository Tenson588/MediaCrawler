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
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from config.db_config import SQLITE_DB_PATH
from api.services.brand_monitor import (
    BRAND_MONITOR_KEYWORDS,
    BRAND_MONITOR_SEEDS,
    brand_monitor_manager,
)
from api.services.report_pdf import generate_report_pdf

router = APIRouter(prefix='/reports', tags=['reports'])
REPORT_DIR = Path(__file__).resolve().parents[2] / 'data' / 'reports'
TZ = ZoneInfo('Asia/Shanghai')
DEAL_WORDS = ('优惠', '折扣', '折起', '满减', '满赠', '闪购', '特卖', '清仓', '到手价', '活动价', '双11', '双十一')
METADATA_PREFIXES = ('生成时间', '发布时间范围', '关键词', '搜索词', '排序', '原始结果', '本期高相关', '匹配笔记', '说明')
PRICE_PATTERN = re.compile(
    r'(?:满\s*)?[\d,]+\s*(?:元\s*)?减\s*[\d,]+\s*(?:元)?'
    r'|\d+(?:\.\d+)?\s*[—–-]\s*\d+(?:\.\d+)?\s*折(?:起)?'
    r'|\d+(?:\.\d+)?\s*折(?:起)?'
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
ReportScope = Literal['dealer', 'brand']


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


def report_directory(scope: ReportScope) -> Path:
    return REPORT_DIR / 'brand' if scope == 'brand' else REPORT_DIR


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


def scoped_notes(rows: list[dict], scope: ReportScope) -> list[dict]:
    return [row for row in rows if is_brand_note(row) == (scope == 'brand')]


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
    for match in re.finditer(r'(\d+(?:\.\d+)?)\s*[—–-]?\s*(\d+(?:\.\d+)?)?\s*折(?:起)?', text):
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
    strongest: tuple[float, str, str] | None = None
    latest: tuple[datetime, str, str] | None = None
    fallback: tuple[str, str] | None = None

    for section in sections:
        title = _plain_text(section['title'])
        section_text = '\n'.join(section['lines'])
        if fallback is None and title != '报告概览' and any(word in title for word in DEAL_WORDS):
            fallback = (title, title)
        for line in section['lines']:
            plain_line = _plain_text(line)
            if not plain_line:
                continue
            if any(plain_line.startswith(prefix + marker) for prefix in METADATA_PREFIXES for marker in ('：', ':')):
                continue
            scored = _discount_score(plain_line)
            if scored and (strongest is None or scored[0] > strongest[0]):
                strongest = (scored[0], title, plain_line)
            if fallback is None and any(word in plain_line for word in DEAL_WORDS):
                fallback = (title, plain_line)
        published_result = _published_at(section_text, default_year)
        if published_result and (latest is None or published_result[0] > latest[0]):
            published, matched_line = published_result
            detail = _plain_text(matched_line)
            if detail.startswith('发布时间'):
                detail = next((_plain_text(line) for line in section['lines'] if any(word in line for word in DEAL_WORDS)), '')
            latest = (published, title, detail)

    parts: list[str] = []
    deal = strongest or ((0, *fallback) if fallback else None)
    if deal:
        _, title, detail = deal
        prefix = '最大优惠' if strongest else '优惠焦点'
        if detail == title:
            parts.append(f'{prefix}：“{title}”被列为促销候选线索。')
        else:
            parts.append(f'{prefix}：“{title}”中提到{detail[:100]}。')
    if latest:
        published, title, detail = latest
        movement = f'最新动向：{published:%Y-%m-%d %H:%M} 发布的“{title}”'
        if detail and (not deal or detail != deal[2]):
            movement += f'提到{detail[:70]}'
        parts.append(movement + '。')
    if not parts:
        return '当前报告没有提取到明确的优惠数字或最新活动动向，建议查看正文并核实活动有效期、库存和购买渠道。'
    return '\n'.join(parts)


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
    keywords = sorted({row['source_keyword'] for row in rows if row['source_keyword']})
    if not keywords:
        if scope == 'brand':
            keywords = list(BRAND_MONITOR_KEYWORDS)
        else:
            from config.base_config import KEYWORDS
            keywords = [word.strip() for word in KEYWORDS.split(',') if word.strip()]
    return {'scope': scope, 'keywords': keywords, 'total': len(rows),
            'last_collected': date_text(max((seconds(row['last_modify_ts']) for row in rows), default=0)),
            'notes': [{**row, 'published': date_text(row['time'])} for row in rows]}


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


@router.post('/generate')
def generate(request: GenerateRequest):
    now = datetime.now(TZ)
    cutoff = (now - timedelta(days=request.days)).timestamp()
    terms = [term.strip() for term in re.split('[,，\n]', request.keywords) if term.strip()]
    all_rows = scoped_notes(notes(), request.scope)
    rows = [row for row in all_rows if cutoff <= seconds(row['time']) <= now.timestamp()
            and (not terms or any(term.casefold() in ' '.join(str(row.get(key) or '') for key in
                ('title', 'desc', 'source_keyword')).casefold() for term in terms))]
    signals = ('特卖', '清仓', '折扣', '满减', '闪购', '优惠', '双11', '双十一', '内购', '奥莱')
    candidates = [row for row in rows if any(term in (row['title'] or '') + (row['desc'] or '') for term in signals)]
    scope_title = '品牌' if request.scope == 'brand' else '经销商'
    lines = [f'# 小红书{scope_title}监控报告 · {now:%Y-%m-%d %H:%M}', '', '## 促销候选线索', '',
             f'共发现 **{len(candidates)} 条**促销候选线索。', '']
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
        f'- 匹配笔记：{len(rows)} 条；包含促销词的候选线索：{len(candidates)} 条',
        '',
        '> 本报告基于本地已采集数据生成，不会发起新的采集。促销词命中仅代表候选线索，活动有效期、价格和线上购买渠道需要核实。',
    ])
    content = '\n'.join(lines) + '\n'
    summary = build_report_summary(content)
    content_lines = content.splitlines()
    content_lines[1:1] = ['', '## 重点摘要', '', summary_markdown(summary)]
    content = '\n'.join(content_lines) + '\n'
    directory = report_directory(request.scope)
    directory.mkdir(parents=True, exist_ok=True)
    name = f'{now:%Y%m%d-%H%M%S}-{uuid4().hex[:8]}.md'
    temporary = directory / (name + '.tmp')
    temporary.write_text(content, encoding='utf-8')
    temporary.replace(directory / name)
    return {'name': name, 'scope': request.scope, 'content': content, 'summary': summary, 'matched': len(rows)}


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
    return FileResponse(report_path(name, scope), filename=name, media_type='text/markdown; charset=utf-8')


@router.get('/{name}/pdf')
def download_pdf(name: str, scope: ReportScope = 'dealer'):
    path = report_path(name, scope)
    content = path.read_text(encoding='utf-8')
    filename = f'{path.stem}.pdf'
    return Response(
        generate_report_pdf(content, build_report_summary(content)),
        media_type='application/pdf',
        headers={
            'Content-Disposition': (
                f'attachment; filename="report.pdf"; filename*=UTF-8\'\'{quote(filename)}'
            )
        },
    )


@router.get('/{name}')
def detail(name: str, scope: ReportScope = 'dealer'):
    content = report_path(name, scope).read_text(encoding='utf-8')
    return {'name': name, 'scope': scope, 'content': content, 'summary': build_report_summary(content)}
