import sqlite3
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers import reports
from api.services.brand_monitor import BRAND_MONITOR_KEYWORDS, BRAND_MONITOR_SEEDS, BrandMonitorManager
from api.services import report_pdf


def test_report_lifecycle(tmp_path, monkeypatch):
    database = tmp_path / 'notes.db'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE xhs_note (note_id TEXT,title TEXT,desc TEXT,nickname TEXT,time INTEGER,last_modify_ts INTEGER,source_keyword TEXT)')
        now = int(datetime.now().timestamp())
        db.executemany('INSERT INTO xhs_note VALUES (?,?,?,?,?,?,?)', [
            ('abc', '冷山双十一优惠', '满1000减120；W27新款不参加', '商家', now * 1000, now, '冷山'),
            ('old', '冷山清仓', '过期笔记', '商家', (now - 86400 * 40) * 1000, now, '冷山'),
            ('other', '其他品牌', '', '商家', now * 1000, now, '其他'),
        ])
    monkeypatch.setattr(reports, 'SQLITE_DB_PATH', str(database))
    monkeypatch.setattr(reports, 'REPORT_DIR', tmp_path / 'reports')
    app = FastAPI()
    app.include_router(reports.router)
    client = TestClient(app)
    assert client.get('/reports/overview').json()['total'] == 3
    result = client.post('/reports/generate', json={'keywords': '冷山', 'days': 7})
    assert result.status_code == 200
    report = result.json()
    assert report['matched'] == 1
    assert report['summary']
    assert '## 重点摘要' in report['content']
    assert '共发现 **1 条**促销候选线索。' in report['content']
    assert '### 1. 冷山双十一优惠' in report['content']
    assert '- 价格与活动条件：' in report['content']
    assert '**优惠**：满1000减120' in report['content']
    assert '**限制**：W27新款不参加' in report['content']
    assert report['content'].rfind('## 备注') > report['content'].rfind('## 全部匹配笔记')
    assert report['content'].rfind('- 生成时间：') > report['content'].rfind('## 备注')
    assert report['content'].rstrip().endswith('活动有效期、价格和线上购买渠道需要核实。')
    assert '过期笔记' not in report['content']
    assert '满1000减120；W27新款不参加' in report['content']
    assert len(client.get('/reports?q=满1000').json()['reports']) == 1
    assert client.get('/reports?q=无匹配').json()['reports'] == []
    detail = client.get('/reports/' + report['name']).json()
    assert detail['content'] == report['content']
    assert detail['summary'] == report['summary']
    assert client.get('/reports/' + report['name'] + '/download').status_code == 200
    pdf = client.get('/reports/' + report['name'] + '/pdf')
    assert pdf.status_code == 200
    assert pdf.headers['content-type'] == 'application/pdf'
    assert 'filename="report.pdf"' in pdf.headers['content-disposition']
    assert "filename*=UTF-8''" in pdf.headers['content-disposition']
    assert pdf.content.startswith(b'%PDF-')
    assert len(pdf.content) > 2_000
    assert client.get('/reports/missing.md').status_code == 404
    assert client.post('/reports/generate', json={'days': 0}).status_code == 422
    assert client.post('/reports/generate', json={'keywords': '不存在', 'days': 7}).json()['matched'] == 0


def test_empty_database_and_path_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, 'SQLITE_DB_PATH', str(tmp_path / 'missing.db'))
    assert reports.overview()['total'] == 0
    import pytest
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        reports.report_path('../secret.md')


def test_brand_records_and_reports_are_isolated_from_dealers(tmp_path, monkeypatch):
    database = tmp_path / 'notes.db'
    now = int(datetime.now().timestamp())
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE xhs_note (note_id TEXT,title TEXT,desc TEXT,nickname TEXT,time INTEGER,last_modify_ts INTEGER,source_keyword TEXT)')
        db.executemany('INSERT INTO xhs_note VALUES (?,?,?,?,?,?,?)', [
            ('brand-1', 'Burton 官方闪购', '部分款式 7 折，27款不参加', '品牌账号', now * 1000, now, 'Burton官方'),
            ('dealer-1', '冷山商城特卖', '雪具满1000减100', '经销商', now * 1000, now, '冷山商城'),
        ])
    monkeypatch.setattr(reports, 'SQLITE_DB_PATH', str(database))
    monkeypatch.setattr(reports, 'REPORT_DIR', tmp_path / 'reports')
    app = FastAPI()
    app.include_router(reports.router)
    client = TestClient(app)

    dealer = client.get('/reports/overview?scope=dealer').json()
    brand = client.get('/reports/overview?scope=brand').json()
    assert dealer['total'] == 1
    assert dealer['notes'][0]['note_id'] == 'dealer-1'
    assert brand['total'] == 1
    assert brand['notes'][0]['note_id'] == 'brand-1'

    generated = client.post('/reports/generate', json={
        'scope': 'brand', 'keywords': 'Burton', 'days': 7,
    }).json()
    assert generated['scope'] == 'brand'
    assert generated['matched'] == 1
    assert generated['content'].startswith('# 小红书品牌监控报告')
    assert client.get('/reports?scope=dealer').json()['reports'] == []
    assert len(client.get('/reports?scope=brand').json()['reports']) == 1
    assert client.get(f"/reports/{generated['name']}?scope=brand").status_code == 200
    assert client.get(f"/reports/{generated['name']}").status_code == 404


def test_report_summary_selects_strongest_deal_and_latest_update():
    content = '''# 特卖日报

## 蕉下内购特卖
- 优惠：微瑕款 1 折起、全新良品 2 折起
- 发布时间：2026-10-17 09:01

## 南京二手雪具市集
- 优惠：现场有雪具特卖
- 发布时间：2026-10-19 11:43
'''
    summary = reports.build_report_summary(content)
    assert '最大优惠' in summary
    assert '1 折起' in summary
    assert '最新动向：2026-10-19 11:43' in summary
    assert '南京二手雪具市集' in summary
    assert '\n' in summary
    markdown = reports.summary_markdown(summary)
    assert '- **最大优惠：**' in markdown
    assert '**1 折起**' in markdown
    assert '- **最新动向：**' in markdown


def test_existing_report_summary_uses_available_discount():
    content = '''# 监控报告

- 生成时间：2026-10-19 18:00

## 冷山活动
- 优惠：满 8,000 减 1,050

## 新线索
- BURTON 闪购特卖会：10 月 19 日出现新帖
'''
    summary = reports.build_report_summary(content)
    assert '满 8,000 减 1,050' in summary
    assert '最新动向' in summary


def test_report_summary_ignores_keyword_metadata():
    content = '''# 自动报告

- 关键词：滑雪特卖, 双十一

## Spaders 双11开板季
- 作者：品牌账号；发布时间：2026-10-20 08:30
- 正文没有公布具体折扣
'''
    summary = reports.build_report_summary(content)
    assert '关键词' not in summary
    assert 'Spaders 双11开板季' in summary


def test_deal_conditions_can_use_related_same_brand_note():
    current = {
        'note_id': 'current', 'title': 'BURTON（伯顿）闪购特卖会',
        'desc': '伯顿全球单板滑雪品牌。', 'source_keyword': '滑雪 特卖',
        'time': 1_800_000_000_000,
    }
    related = {
        'note_id': 'related', 'title': 'Burton双十一活动即将来袭',
        'desc': '装备原有折扣上\n2件9折｜3件直接85折\nW27新款不参与活动\n东莞奥莱Burton门店',
        'source_keyword': '滑雪 特卖', 'time': 1_799_900_000_000,
    }
    conditions, source = reports.deal_condition_context(current, [current, related])
    assert source == related
    assert ('优惠', '2件9折｜3件直接85折') in conditions
    assert ('限制', 'W27新款不参与活动') in conditions
    assert ('渠道/地点', '东莞奥莱Burton门店') in conditions


def test_pdf_markup_prints_full_url_and_cleans_topic_markers():
    url = 'https://www.xiaohongshu.com/explore/example'
    markup = report_pdf._inline_markup(f'原文：{url} #滑雪装备[话题]#')
    assert f'href="{url}"' in markup
    assert f'>{url}</link>' in markup
    assert '打开原文' not in markup
    assert '[话题]' not in markup
    assert markup.endswith(' 滑雪装备')


def test_report_page_renders_formatted_content():
    page = (reports.REPORT_DIR.parents[1] / 'api' / 'static' / 'reports.html').read_text(encoding='utf-8')
    assert '<div id="content" class="report-content"></div>' in page
    assert 'function reportHtml(content)' in page
    assert "replace(/\\[话题\\]/g,'').replace(/#/g,'')" in page


def test_report_page_separates_dealer_and_brand_workspaces():
    page = (reports.REPORT_DIR.parents[1] / 'api' / 'static' / 'reports.html').read_text(encoding='utf-8')
    assert 'data-tab="dealer">经销商</button>' in page
    assert 'data-tab="brand">品牌</button>' in page
    assert 'id="dealer-panel" role="tabpanel"' in page
    assert 'id="brand-panel" role="tabpanel"' in page
    assert 'function switchWorkspaceTab(name,focus=false)' in page
    assert 'id="brand-generate"' in page
    assert 'id="brand-history"' in page
    assert 'id="brand-notes"' in page
    assert "scope:'brand'" in page


def test_report_page_runtime_checks_are_collapsible_and_status_colored():
    page = (reports.REPORT_DIR.parents[1] / 'api' / 'static' / 'reports.html').read_text(encoding='utf-8')
    assert 'id="runtime-toggle" aria-expanded="false"' in page
    assert 'id="runtime-details" hidden' in page
    assert 'function setRuntimeExpanded(expanded)' in page
    assert "$('runtime-card').className='card service-card '+stateClass" in page


def test_brand_monitor_has_separate_detail_and_search_phases():
    manager = BrandMonitorManager()
    phases = manager.phase_configs()
    assert [phase[0] for phase in phases] == ['known_post', 'brand_search']
    assert BRAND_MONITOR_SEEDS[0]['note_id'] in phases[0][2].specified_ids
    assert phases[1][2].keywords == ','.join(BRAND_MONITOR_KEYWORDS)
    assert phases[1][2].save_option.value == 'sqlite'
    assert phases[1][2].enable_comments is False


def test_brand_monitor_status_endpoint_lists_brands_and_seed(monkeypatch):
    snapshot = {
        'status': 'idle', 'phase': None, 'message': 'ready',
        'started_at': None, 'finished_at': None,
        'brands': ['Burton'], 'keywords': ['Burton官方'],
        'seeds': [{'title': 'Burton 官方活动', 'url': 'https://xhslink.cn/example'}],
        'last_log': None,
    }
    monkeypatch.setattr(reports.brand_monitor_manager, 'snapshot', lambda: snapshot)
    app = FastAPI()
    app.include_router(reports.router)
    response = TestClient(app).get('/reports/brand-monitor')
    assert response.status_code == 200
    assert response.json() == snapshot
