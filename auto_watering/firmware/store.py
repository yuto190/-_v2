"""設定・状態・ログのフラッシュ保存。MicroPython / CPython 両対応。"""
import json
import os

CONFIG_FILE = "config.json"
STATE_FILE = "state.json"
LOG_FILE = "log.csv"

DEFAULT_STATE = {
    "dry_since": None,
    "last_water_end": None,
    "day_key": None,
    "waterings_today": 0,
    "last_pct": None,
    "last_action": None,
}


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    try:
        os.remove(path)
    except OSError:
        pass
    os.rename(tmp, path)


def load_config():
    cfg = _read_json(CONFIG_FILE, None)
    if cfg is None:
        raise RuntimeError("config.json が読めません")
    return cfg


def save_config(cfg):
    _write_json(CONFIG_FILE, cfg)


def load_state():
    st = dict(DEFAULT_STATE)
    st.update(_read_json(STATE_FILE, {}))
    return st


def save_state(st):
    _write_json(STATE_FILE, st)


def _file_size(path):
    try:
        return os.stat(path)[6]
    except OSError:
        return 0


def append_log(line, max_bytes=200000):
    """CSV 1行追記。上限を超えたら後半だけ残して切り詰める。"""
    if _file_size(LOG_FILE) > max_bytes:
        try:
            with open(LOG_FILE) as f:
                f.seek(max_bytes // 2)
                f.readline()  # 行の途中を捨てる
                rest = f.read()
            with open(LOG_FILE, "w") as f:
                f.write(rest)
        except OSError:
            pass
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def tail_log(max_lines=50):
    try:
        with open(LOG_FILE) as f:
            lines = f.read().split("\n")
    except OSError:
        return []
    lines = [l for l in lines if l]
    return lines[-max_lines:]
