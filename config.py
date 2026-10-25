# -*- coding: utf-8 -*-
"""
配置持久化：%APPDATA%\Win11Dash\config.json
保存布局/主题/缩放/位置/透明度/置顶，重启后完整恢复。
"""
import json
import os
import tempfile

CONF_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), "Win11Dash")
CONF_PATH = os.path.join(CONF_DIR, "config.json")

DEFAULTS = {
    "theme": "dark",    # dark / light / amber
    "layout": "h",      # h(横版 620x210) / v(竖版 210x660)
    "scale": 1.0,       # 0.5 ~ 3.0
    "opacity": 1.0,     # 0.3 ~ 1.0
    "topmost": True,
    "pos_h": None,      # [x, y] 横版窗口位置
    "pos_v": None,      # [x, y] 竖版窗口位置
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONF_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in DEFAULTS:
                if k in data:
                    cfg[k] = data[k]
    except Exception:
        pass
    # 容错与钳制
    if cfg["theme"] not in ("dark", "light", "amber", "glow"):
        cfg["theme"] = "dark"
    if cfg["layout"] not in ("h", "v"):
        cfg["layout"] = "h"
    try:
        cfg["scale"] = min(3.0, max(0.5, float(cfg["scale"])))
    except Exception:
        cfg["scale"] = 1.0
    try:
        cfg["opacity"] = min(1.0, max(0.3, float(cfg["opacity"])))
    except Exception:
        cfg["opacity"] = 1.0
    cfg["topmost"] = bool(cfg["topmost"])
    for k in ("pos_h", "pos_v"):
        p = cfg[k]
        if not (isinstance(p, (list, tuple)) and len(p) == 2):
            cfg[k] = None
    return cfg


def save_config(cfg):
    """原子写入（临时文件 + os.replace），失败静默——配置写不进去不影响运行"""
    try:
        os.makedirs(CONF_DIR, exist_ok=True)
        data = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
        fd, tmp = tempfile.mkstemp(dir=CONF_DIR, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONF_PATH)
    except Exception:
        pass
