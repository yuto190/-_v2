"""
散水判定ロジック（純粋Python）。

MicroPython でも CPython でも動くように、ハードウェア依存のimportは一切しない。
tests/test_logic.py で CPython 上の単体テストが走る。

時刻は「秒」の整数（time.time() の値）で受け取り、差分だけを使う。
"""

# plan() が返すアクション
WET = "wet"                    # 十分湿っている。何もしない
DRY_WAIT = "dry_wait"          # しきい値を下回ったが、確認待ち時間が未経過
COOLDOWN = "cooldown"          # 前回散水から min_interval 未経過
DAILY_LIMIT = "daily_limit"    # 1日の散水回数上限
OUTSIDE_HOURS = "outside_hours"  # 散水許可時間帯の外
LOW_BATTERY = "low_battery"    # 電池電圧が低い
WATER = "water"                # 散水を開始する


def moisture_pct(raw_mv, raw_dry, raw_wet):
    """センサー出力(mV)を 0–100% に線形変換する。

    静電容量式センサーは「乾くほど電圧が高い」ので raw_dry > raw_wet。
    範囲外は 0 / 100 にクランプする。
    """
    if raw_dry == raw_wet:
        return 0
    pct = (raw_dry - raw_mv) * 100.0 / (raw_dry - raw_wet)
    if pct < 0:
        pct = 0
    if pct > 100:
        pct = 100
    return int(pct + 0.5)


def _in_hours(hour, window):
    """window=[start, end]。start<=end なら [start,end)、start>end は日跨ぎ。"""
    start, end = window
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def plan(cfg, state, now, pct, hour=None, batt_mv=None):
    """散水するかどうかを決める。state は書き換えて返す。

    cfg (dict):
      threshold_on_pct            散水開始しきい値（これ未満で「乾いた」）
      dry_confirm_delay_min       乾いたと判定してから散水開始までの待ち時間
      min_interval_between_waterings_min  前回散水終了から次回開始までの最短間隔
      max_waterings_per_day       1日の散水回数上限（0 なら無制限）
      allowed_hours               [start, end] または None（hour が None なら無視）
      low_battery_mv              これ未満なら散水しない（batt_mv が None なら無視）
    state (dict):
      dry_since, last_water_end, day_key, waterings_today
    """
    thr = cfg["threshold_on_pct"]

    if pct >= thr:
        if state.get("dry_since") is not None:
            state["dry_since"] = None
        return WET

    if state.get("dry_since") is None:
        state["dry_since"] = now
        return DRY_WAIT

    if now - state["dry_since"] < cfg["dry_confirm_delay_min"] * 60:
        return DRY_WAIT

    lwe = state.get("last_water_end")
    if lwe is not None and now - lwe < cfg["min_interval_between_waterings_min"] * 60:
        return COOLDOWN

    # 日付が変わったら回数をリセット（86400秒単位。時計が合っていなくても相対で機能する）
    day_key = now // 86400
    if state.get("day_key") != day_key:
        state["day_key"] = day_key
        state["waterings_today"] = 0
    limit = cfg.get("max_waterings_per_day", 0)
    if limit and state.get("waterings_today", 0) >= limit:
        return DAILY_LIMIT

    window = cfg.get("allowed_hours")
    if window and hour is not None and not _in_hours(hour, window):
        return OUTSIDE_HOURS

    lb = cfg.get("low_battery_mv", 0)
    if lb and batt_mv is not None and batt_mv < lb:
        return LOW_BATTERY

    return WATER


def watering_steps(wcfg):
    """散水パターンを (動作, 秒) のリストに展開する。

    wcfg:
      spray_sec  1回の散水時間
      pause_sec  散水と散水の間の休憩
      repeats    繰り返し回数
    例: spray=180, pause=300, repeats=2 → [("spray",180),("pause",300),("spray",180)]
    最後の休憩は入れない。
    """
    steps = []
    n = max(1, int(wcfg.get("repeats", 1)))
    for i in range(n):
        steps.append(("spray", int(wcfg["spray_sec"])))
        if i < n - 1 and wcfg.get("pause_sec", 0) > 0:
            steps.append(("pause", int(wcfg["pause_sec"])))
    return steps


def finish_watering(state, now):
    """散水完了後の状態更新。"""
    state["last_water_end"] = now
    state["dry_since"] = None
    state["waterings_today"] = state.get("waterings_today", 0) + 1
    return state
