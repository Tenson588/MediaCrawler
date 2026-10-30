# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/config/xhs_config.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#

# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。


# Xiaohongshu platform configuration

# 小红书采用低速采集间隔，减少连续请求的负担；不代表人工行为或免于风控。
# 访问受限时程序会立即停止本轮任务，不会自动重试验证码请求。
XHS_REQUEST_INTERVAL_MIN_SEC = 30
XHS_REQUEST_INTERVAL_MAX_SEC = 60

# 整次搜索任务的笔记详情尝试数和搜索页调用预算（不是每个关键词）。
# 单篇详情可能调用 API、重试或 HTML 兜底，因此这不是底层 HTTP 请求总数。
XHS_MAX_NOTES_PER_RUN = 15
XHS_MAX_SEARCH_REQUESTS_PER_RUN = 12
# SQLite 中最近 24 小时已保存的笔记不再请求详情；设为 0 可显式刷新。
XHS_SKIP_RECENT_HOURS = 24

# Sorting method, the specific enumeration value is in media_platform/xhs/field.py
SORT_TYPE = "time_descending"

# Specify the note URL list, which must carry the xsec_token parameter
XHS_SPECIFIED_NOTE_URL_LIST = [
    "https://www.xiaohongshu.com/explore/6aa3d827000000002802c5c8?xsec_token=YBIq8sY-0_K3BACQ7z57J9xJcdflrd8BAf5_zBeJtFMOQ=&xsec_source=pc_creatormng"
    # ........................
]

# Specify the creator URL list, which needs to carry xsec_token and xsec_source parameters.

XHS_CREATOR_ID_LIST = [
    "https://www.xiaohongshu.com/user/profile/5f58bd990000000001003753?xsec_token=ABYVg1evluJZZzpMX-VWzchxQ1qSNVW3r-jOEnKqMcgZw=&xsec_source=pc_search"
    # ........................
]
