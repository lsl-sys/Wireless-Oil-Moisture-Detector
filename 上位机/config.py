# -*- coding: utf-8 -*-
"""用户偏好持久化（%APPDATA%/OilMoistureMonitor/config.json）

与界面无关的纯配置层：启动偏好、CSV 切分周期、历史记录上限。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "OilMoistureMonitor"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "auto_fullscreen": True,
    "csv_split_minutes": 0,   # CSV 自动切分时长（分钟，0=不切）
    "max_records": 20000,     # 历史记录条数上限
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            cfg.update({k: data[k] for k in DEFAULT_CONFIG if k in data})
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
