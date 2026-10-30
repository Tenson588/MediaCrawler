"""Resolve one explicit login profile for collection and manual verification."""

import os
import re
from pathlib import Path

import config


def xhs_profile_dir(project_root: Path, *, cdp: bool | None = None) -> Path:
    profile = os.environ.get("MEDIACRAWLER_XHS_PROFILE", "").strip()
    if profile and not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", profile):
        raise ValueError("MEDIACRAWLER_XHS_PROFILE 只能包含 1–40 位字母、数字、下划线或连字符")
    use_cdp = config.ENABLE_CDP_MODE if cdp is None else cdp
    name = ("cdp_" if use_cdp else "") + config.USER_DATA_DIR % "xhs"
    if profile:
        name += "_" + profile
    return project_root / "browser_data" / name
