import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers import reports
from api.services.brand_monitor import BRAND_MONITOR_KEYWORDS, BRAND_MONITOR_SEEDS, BrandMonitorManager
from api.services.dealer_monitor import DealerMonitorManager
from api.services.rev_monitor import REV_MONITOR_KEYWORDS, REV_MONITOR_TOPICS, RevMonitorManager
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
    overview = client.get('/reports/overview').json()
    assert overview['total'] == 3
    assert overview['database_timestamp'].count(':') == 2
    result = client.post('/reports/generate', json={'keywords': '冷山', 'days': 7})
    assert result.status_code == 200
    report = result.json()
    assert report['matched'] == 1
    assert report['summary']
    assert '## 重点摘要' not in report['content']
    assert '发布者：商家' in report['summary']
    assert 'https://www.xiaohongshu.com/explore/abc' in report['summary']
    assert report['candidates'] == 1
    assert report['filtered_overseas'] == 0
    assert '共发现 **1 条**中国大陆促销候选线索。' in report['content']
    assert '### 1. 冷山双十一优惠' in report['content']
    assert '- 本条原文中的价格与活动条件：' in report['content']
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
    markdown = client.get('/reports/' + report['name'] + '/download')
    assert markdown.status_code == 200
    assert '## 重点摘要' not in markdown.text
    pdf = client.get('/reports/' + report['name'] + '/pdf')
    assert pdf.status_code == 200
    assert pdf.headers['content-type'] == 'application/pdf'
    assert 'filename="report.pdf"' in pdf.headers['content-disposition']
    assert "filename*=UTF-8''" in pdf.headers['content-disposition']
    assert pdf.content.startswith(b'%PDF-')
    assert len(pdf.content) > 2_000
    deleted = client.delete('/reports/' + report['name'])
    assert deleted.status_code == 200
    assert deleted.json()['deleted'] is True
    assert client.get('/reports/' + report['name']).status_code == 404
    assert client.get('/reports/missing.md').status_code == 404
    assert client.post('/reports/generate', json={'days': 0}).status_code == 422
    assert client.post('/reports/generate', json={'keywords': '不存在', 'days': 7}).json()['matched'] == 0


def test_generate_filters_overseas_candidates_and_applies_dealer_priority(tmp_path, monkeypatch):
    database = tmp_path / 'notes.db'
    now = int(datetime.now().timestamp())
    records = [
        ('other', '普通雪具特卖', '上海门店优惠', '普通商家', now * 1000, now, '滑雪 特卖'),
        ('sanfo', '三夫攀山鼠特卖', '北京门店折扣', '三夫户外', (now - 10) * 1000, now, '滑雪 特卖'),
        ('spaders', 'SPADERS 黑桃优惠', '线上商城双十一', '黑桃雪具', (now - 20) * 1000, now, '黑桃雪具'),
        ('aotian', '傲天雪具特卖', '雪板限时优惠', '傲天雪具', (now - 30) * 1000, now, '傲天特卖'),
        ('cold', '冷山商城清仓', '滑雪装备折扣', '冷山商城', (now - 40) * 1000, now, '冷山商城'),
        ('sweden', '瑞典攀山鼠特卖', '瑞典现场 2500 克朗', '海外买手', now * 1000, now, '滑雪 特卖'),
        ('canada', 'Calgary Ski Sale 特卖', 'Priddis, Canada 门店活动', '海外商家', now * 1000, now, '滑雪 特卖'),
    ]
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE xhs_note (note_id TEXT,title TEXT,desc TEXT,nickname TEXT,time INTEGER,last_modify_ts INTEGER,source_keyword TEXT)')
        db.executemany('INSERT INTO xhs_note VALUES (?,?,?,?,?,?,?)', records)
    monkeypatch.setattr(reports, 'SQLITE_DB_PATH', str(database))
    monkeypatch.setattr(reports, 'REPORT_DIR', tmp_path / 'reports')
    app = FastAPI()
    app.include_router(reports.router)
    client = TestClient(app)

    response = client.post('/reports/generate', json={'days': 7})
    assert response.status_code == 200
    generated = response.json()
    assert generated['candidates'] == 5
    assert generated['filtered_overseas'] == 2
    candidate_section = generated['content'].split('## 全部匹配笔记', 1)[0]
    assert '瑞典攀山鼠特卖' not in candidate_section
    assert 'Calgary Ski Sale' not in candidate_section
    ordered_ids = ['cold', 'aotian', 'spaders', 'sanfo', 'other']
    positions = [candidate_section.index(f'/explore/{note_id}') for note_id in ordered_ids]
    assert positions == sorted(positions)
    assert '已过滤 **2 条**明确属于非中国大陆的候选信息。' in generated['content']
    assert '- 排序：冷山 → 傲天 → 黑桃/SPADERS → 三夫/攀山鼠 → 其他；同级按发布时间倒序' in generated['content']


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
            ('rev-1', 'REV X1头盔使用问题', '撞击后外壳开裂', '用户', now * 1000, now, 'REV头盔问题'),
        ])
    monkeypatch.setattr(reports, 'SQLITE_DB_PATH', str(database))
    monkeypatch.setattr(reports, 'REPORT_DIR', tmp_path / 'reports')
    app = FastAPI()
    app.include_router(reports.router)
    client = TestClient(app)

    dealer = client.get('/reports/overview?scope=dealer').json()
    brand = client.get('/reports/overview?scope=brand').json()
    rev = client.get('/reports/overview?scope=rev').json()
    assert dealer['total'] == 1
    assert dealer['notes'][0]['note_id'] == 'dealer-1'
    assert brand['total'] == 1
    assert brand['notes'][0]['note_id'] == 'brand-1'
    assert rev['total'] == 1
    assert rev['notes'][0]['note_id'] == 'rev-1'
    assert rev['issue_count'] == 1

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


def test_rev_report_groups_models_and_keeps_safety_claims_as_candidates(tmp_path, monkeypatch):
    database = tmp_path / 'notes.db'
    now = int(datetime.now().timestamp())
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE xhs_note (note_id TEXT,title TEXT,desc TEXT,nickname TEXT,time INTEGER,last_modify_ts INTEGER,source_keyword TEXT)')
        db.executemany('INSERT INTO xhs_note VALUES (?,?,?,?,?,?,?)', [
            ('rev-x1', 'REV X1头盔避雷', '滑雪摔倒撞击后外壳开裂，卡扣也松动了，正在联系售后', '雪友甲', now * 1000, now, 'REV头盔问题'),
            ('rev-x2', 'REV X2头盔使用感受', '护目镜容易起雾，佩戴有点夹头', '雪友乙', (now - 10) * 1000, now, 'REV头盔'),
            ('rev-show', 'REV头盔开箱', '新头盔配色与外观展示', '雪友丙', (now - 20) * 1000, now, 'REV头盔'),
            ('dealer', '普通雪具特卖', '优惠活动', '商家', now * 1000, now, '滑雪特卖'),
        ])
    monkeypatch.setattr(reports, 'SQLITE_DB_PATH', str(database))
    monkeypatch.setattr(reports, 'REPORT_DIR', tmp_path / 'reports')
    app = FastAPI()
    app.include_router(reports.router)
    client = TestClient(app)

    overview = client.get('/reports/overview?scope=rev').json()
    assert overview['total'] == 3
    assert overview['issue_count'] == 2
    x1 = next(note for note in overview['notes'] if note['note_id'] == 'rev-x1')
    assert x1['rev_issue']['risk'] == '高关注'
    assert x1['rev_issue']['models'] == ['X1']
    assert '结构/撞击保护' in x1['rev_issue']['groups']

    response = client.post('/reports/generate', json={'scope': 'rev', 'days': 7})
    assert response.status_code == 200
    generated = response.json()
    assert generated['scope'] == 'rev'
    assert generated['matched'] == 3
    assert generated['candidates'] == 2
    assert generated['high_attention'] == 1
    assert generated['content'].startswith('# REV头盔风险监控报告')
    assert '## 问题候选线索' in generated['content']
    assert '### 1. REV X1头盔避雷' in generated['content']
    assert '**X1**：1 条问题候选；高关注 1 条' in generated['content']
    assert '不能单独证明某一型号存在安全隐患' in generated['content']
    assert client.get('/reports?scope=dealer').json()['reports'] == []
    assert len(client.get('/reports?scope=rev').json()['reports']) == 1
    detail = client.get(f"/reports/{generated['name']}?scope=rev").json()
    assert '用户帖子仅作为风险线索' in detail['summary']
    assert 'X1' in detail['summary']
    pdf = client.get(f"/reports/{generated['name']}/pdf?scope=rev")
    assert pdf.status_code == 200
    assert pdf.content.startswith(b'%PDF-')


def test_rev_note_detection_accepts_chinese_text_without_keyword_provenance():
    assert reports.is_rev_note({
        'title': 'REV头盔真实使用感受', 'desc': '佩戴后有松动', 'source_keyword': '滑雪头盔',
    })
    assert reports.is_rev_note({
        'title': '滑雪头盔投诉记录', 'desc': '我使用的Rev X2出现卡扣问题', 'source_keyword': '头盔投诉',
    })
    assert not reports.is_rev_note({
        'title': '滑雪服 review', 'desc': '普通穿搭分享', 'source_keyword': '滑雪装备',
    })


def test_rev_analysis_does_not_treat_positive_product_claims_as_complaints():
    positive = reports.rev_issue_analysis({
        'title': 'REV F7头盔新品',
        'desc': '全尺码展示，防起雾设计，通风良好，佩戴不压头。',
    })
    assert positive['models'] == ['F7']
    assert positive['matched'] is False
    assert positive['risk'] == '未发现问题词'
    hashtag_model = reports.rev_issue_analysis({
        'title': '锐伍头盔使用反馈', 'desc': '#rev头盔A6# 尺码偏小，卡扣松动',
    })
    assert hashtag_model['models'] == ['A6']
    assert hashtag_model['risk'] == '中关注'


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


def test_report_summary_includes_publisher_and_source_without_treating_double_11_as_discount():
    url = 'https://www.xiaohongshu.com/explore/example'
    content = f'''# 自动报告

## 促销候选线索

### 1. 10月15日 - 11月13日
- 作者：多***壮；发布时间：2026-10-20 17:48
- 原文：{url}

老会员代金券自动到账，可叠双11折上折！
'''
    summary = reports.build_report_summary(content)
    assert '发布者：多***壮' in summary
    assert f'原文：{url}' in summary
    assert '最大优惠' not in summary


def test_legacy_embedded_summary_is_removed_from_report_body():
    content = '''# 报告\n\n## 重点摘要\n\n- 重复摘要\n\n## 促销候选线索\n\n正文\n'''
    cleaned = reports.without_embedded_summary(content)
    assert '重点摘要' not in cleaned
    assert '重复摘要' not in cleaned
    assert '## 促销候选线索' in cleaned


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


def test_related_deal_conditions_are_clearly_labeled_as_reference(tmp_path, monkeypatch):
    database = tmp_path / 'notes.db'
    now = int(datetime.now().timestamp())
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE xhs_note (note_id TEXT,title TEXT,desc TEXT,nickname TEXT,time INTEGER,last_modify_ts INTEGER,source_keyword TEXT)')
        db.executemany('INSERT INTO xhs_note VALUES (?,?,?,?,?,?,?)', [
            ('product', '是谁还没有Nitro与冷山的限定fate',
             '各位女神们，你们的梦中情鞋来了！#双十一来冷山#',
             '冷山门店', now * 1000, now, '冷山雪具'),
            ('activity', '冷山双11活动说明',
             'NITRO 活动参考：老会员5张代金券自动到账；部分品牌款式不参加满减叠加',
             '活动参考帖', (now - 86400) * 1000, now, '冷山雪具'),
        ])
    monkeypatch.setattr(reports, 'SQLITE_DB_PATH', str(database))
    monkeypatch.setattr(reports, 'REPORT_DIR', tmp_path / 'reports')
    app = FastAPI()
    app.include_router(reports.router)
    client = TestClient(app)

    generated = client.post('/reports/generate', json={'keywords': '冷山', 'days': 7}).json()
    product_section = generated['content'].split('### 1.', 1)[1].split('### 2.', 1)[0]
    assert '关联活动参考（非本条原文，不能据此判断本产品适用）' in product_section
    assert '**参考来源**' in product_section
    assert '**参考·福利**' in product_section
    assert '老会员5张代金券自动到账' in product_section
    assert '本条原文中的价格与活动条件' not in product_section


def test_pdf_markup_prints_full_url_and_cleans_topic_markers():
    url = 'https://www.xiaohongshu.com/explore/example'
    markup = report_pdf._inline_markup(f'原文：{url} #滑雪装备[话题]#')
    assert f'href="{url}"' in markup
    assert f'>{url}</link>' in markup
    assert '打开原文' not in markup
    assert '[话题]' not in markup
    assert markup.endswith(' 滑雪装备')
    punctuated = report_pdf._inline_markup(f'原文：{url}）。')
    assert f'href="{url}"' in punctuated
    assert f'>{url}</link>）。' in punctuated


def test_pdf_adds_dividers_between_promotion_candidates(monkeypatch):
    real_divider = report_pdf.HRFlowable
    dividers = []

    def tracking_divider(*args, **kwargs):
        dividers.append(kwargs)
        return real_divider(*args, **kwargs)

    monkeypatch.setattr(report_pdf, 'HRFlowable', tracking_divider)
    content = '''# 促销报告

## 促销候选线索

### 1. 冷山优惠
满1000减100

### 2. 傲天特卖
雪具5折起

### 3. 黑桃活动
线上优惠

## 备注
报告说明
'''
    pdf = report_pdf.generate_report_pdf(content, '最大优惠：雪具5折起。')
    assert pdf.startswith(b'%PDF-')
    assert len(dividers) == 2
    assert all(item['color'].hexval() == '0xd6e3e5' for item in dividers)


def test_report_page_renders_formatted_content():
    page = (reports.REPORT_DIR.parents[1] / 'api' / 'static' / 'reports.html').read_text(encoding='utf-8')
    assert '<div id="content" class="report-content"></div>' in page
    assert 'function reportHtml(content)' in page
    assert "replace(/\\[话题\\]/g,'').replace(/#/g,'')" in page
    assert 'function setupBriefingDetails()' in page
    assert 'briefing-toggle' in page
    assert "button.textContent=expanding?'收起详情':'查看详情'" in page


def test_report_page_separates_dealer_and_brand_workspaces():
    page = (reports.REPORT_DIR.parents[1] / 'api' / 'static' / 'reports.html').read_text(encoding='utf-8')
    assert 'data-tab="dealer">经销商</button>' in page
    assert 'data-tab="brand">品牌</button>' in page
    assert 'data-tab="rev">REV 头盔</button>' in page
    assert 'id="dealer-panel" role="tabpanel"' in page
    assert 'id="brand-panel" role="tabpanel"' in page
    assert 'id="rev-panel" role="tabpanel"' in page
    assert 'function switchWorkspaceTab(name,focus=false)' in page
    assert 'id="brand-generate"' in page
    assert 'id="brand-history"' in page
    assert 'id="brand-notes"' in page
    assert 'id="rev-generate"' in page
    assert 'id="rev-history"' in page
    assert 'id="rev-notes"' in page
    assert "scope:'brand'" in page
    assert "scope:'rev'" in page


def test_rev_page_exposes_monitor_progress_risk_fields_and_safety_caveat():
    page = (reports.REPORT_DIR.parents[1] / 'api' / 'static' / 'reports.html').read_text(encoding='utf-8')
    assert 'id="rev-start">执行 REV 专项监控</button>' in page
    assert 'id="rev-progress-track" role="progressbar"' in page
    assert '<th>型号</th><th>问题类型</th><th>关注等级</th>' in page
    assert 'function renderRevMonitor(data)' in page
    assert 'function renderRevNotes()' in page
    assert '不代表已经确认某个型号存在安全隐患' in page


def test_report_page_runtime_checks_are_collapsible_and_status_colored():
    page = (reports.REPORT_DIR.parents[1] / 'api' / 'static' / 'reports.html').read_text(encoding='utf-8')
    assert 'id="runtime-toggle" aria-label="展开服务操作"' in page
    assert 'id="runtime-details" hidden' in page
    assert 'function setRuntimeExpanded(expanded)' in page
    assert "$('runtime-card').className='card service-card '+stateClass" in page
    assert 'id="runtime-restart"' in page
    assert 'id="runtime-stop"' in page
    assert 'id="open-verification">打开验证页面</button>' in page
    assert 'id="access-log" hidden' in page
    assert "types.includes('验证码')" in page
    assert "'/runtime/xhs-verification'" in page
    assert "'/runtime/xhs-verification/close'" in page
    assert '完成验证并关闭窗口' in page
    assert '启动监控面板.command' in page
    assert "location.protocol==='file:'" in page
    assert "API_ORIGIN+'/api/reports'" in page


def test_report_page_has_crawl_progress_deletion_and_five_item_history_pages():
    page = (reports.REPORT_DIR.parents[1] / 'api' / 'static' / 'reports.html').read_text(encoding='utf-8')
    assert '<form class="service-controls" id="service-form" hidden><div class="service-buttons">' in page
    assert '>...</button>' in page
    assert 'id="service-expanded-info" hidden' in page
    assert "$('service-expanded-info').hidden=!expanded" in page
    assert "$('service-form').hidden=!expanded" in page
    assert 'id="dealer-crawl">启动经销商抓取</button>' in page
    assert 'id="database-timestamp">数据版本：读取中…</span>' in page
    assert 'id="dealer-progress-track" role="progressbar"' in page
    assert 'function syncCrawlLock()' in page
    assert 'data-action="delete"' in page
    assert 'aria-label="删除报告"' in page
    assert 'const trashIcon=' in page
    assert 'const HISTORY_PAGE_SIZE=5' in page


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


def test_dealer_monitor_reports_keyword_progress():
    crawler = SimpleNamespace(logs=[], process=None)
    manager = DealerMonitorManager(crawler)
    manager.status = 'running'
    manager.keywords = ['冷山商城', '傲天雪具', '黑桃雪具']
    crawler.logs = [SimpleNamespace(message='[XiaoHongShuCrawler.search] Current search keyword: 傲天雪具')]
    snapshot = manager.snapshot()
    assert snapshot['current_keyword'] == '傲天雪具'
    assert snapshot['completed_keywords'] == 1
    assert 34 <= snapshot['progress'] <= 66
    assert snapshot['total_keywords'] == 3
    manager.status = 'completed'
    assert manager.snapshot()['progress'] == 100


@pytest.mark.asyncio
async def test_rev_monitor_explains_browser_profile_conflict():
    crawler = SimpleNamespace(
        logs=[SimpleNamespace(message='Browser profile is already in use by another instance')],
        process=SimpleNamespace(poll=lambda: 1, returncode=1),
        status='idle',
    )
    manager = RevMonitorManager(crawler)
    manager.status = 'running'
    manager.keywords = list(REV_MONITOR_KEYWORDS)

    await manager._watch()

    assert manager.status == 'error'
    assert '人工验证窗口仍在使用采集登录资料' in manager.message
    assert '请关闭人工验证窗口后再重试' in manager.message


def test_dealer_monitor_status_endpoint(monkeypatch):
    snapshot = {
        'status': 'running', 'message': '正在抓取', 'progress': 42,
        'current_keyword': '冷山商城', 'completed_keywords': 1,
        'total_keywords': 3, 'keywords': ['冷山商城'],
        'started_at': None, 'finished_at': None, 'last_log': None,
    }
    monkeypatch.setattr(reports.dealer_monitor_manager, 'snapshot', lambda: snapshot)
    app = FastAPI()
    app.include_router(reports.router)
    response = TestClient(app).get('/reports/dealer-monitor')
    assert response.status_code == 200
    assert response.json() == snapshot


def test_rev_monitor_uses_fixed_issue_keywords_and_topics():
    crawler = SimpleNamespace(logs=[], process=None)
    manager = RevMonitorManager(crawler)
    manager.status = 'running'
    manager.keywords = list(REV_MONITOR_KEYWORDS)
    crawler.logs = [SimpleNamespace(message='[XiaoHongShuCrawler.search] Current search keyword: REV头盔投诉')]
    snapshot = manager.snapshot()
    assert snapshot['current_keyword'] == 'REV头盔投诉'
    assert snapshot['keywords'] == list(REV_MONITOR_KEYWORDS)
    assert snapshot['topics'] == list(REV_MONITOR_TOPICS)
    assert snapshot['progress'] > 0


def test_rev_monitor_status_endpoint(monkeypatch):
    snapshot = {
        'status': 'idle', 'message': '可单独执行 REV 头盔问题监控。',
        'progress': 0, 'current_keyword': None, 'completed_keywords': 0,
        'total_keywords': 0, 'keywords': [], 'topics': list(REV_MONITOR_TOPICS),
        'started_at': None, 'finished_at': None, 'last_log': None,
    }
    monkeypatch.setattr(reports.rev_monitor_manager, 'snapshot', lambda: snapshot)
    app = FastAPI()
    app.include_router(reports.router)
    response = TestClient(app).get('/reports/rev-monitor')
    assert response.status_code == 200
    assert response.json() == snapshot
