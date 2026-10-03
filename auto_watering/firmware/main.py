"""
土壌水分センサー連動 自動散水コントローラー（ESP32-C3 / MicroPython）

動作:
  1. 起動（電源ON/リセット）時は弁を必ず閉じる。弁の駆動中にリセットされていたら記録し、
     散水間隔（min_interval）を空けてから再挑戦する（ブラウンアウトの繰り返し防止）
  2. 設定ボタンが押されていれば Wi-Fi 設定モード（webcfg.py）
  3. センサーを測定 → logic.plan() で判定 → 必要なら散水 → ログ保存
  4. measure_interval_min だけディープスリープ → 1 に戻る（ディープスリープ復帰では弁は閉じない）
"""
import sys
import time

import machine

import hal
import logic
import store

WDT_MS = 60000


class Context:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state = store.load_state()
        p = cfg["pins"]
        self.led = hal.Led(p["led"])
        self.button = hal.Button(p["config_button"])
        self.sensor = hal.Sensor(p["sensor_adc"], p["sensor_power"],
                                 cfg["sensor"].get("samples", 16), cfg["sensor"].get("warmup_ms", 500))
        self.valve = hal.make_valve(cfg)
        b = cfg.get("battery", {})
        self.battery = hal.Battery(p["battery_adc"], b.get("divider_ratio", 2.0)) if b.get("enabled") else None
        self.wdt = None if cfg.get("debug_no_wdt") else machine.WDT(timeout=WDT_MS)

    def feed(self):
        if self.wdt:
            self.wdt.feed()

    def clock_valid(self):
        return time.localtime()[0] >= 2024

    def read_battery(self):
        return self.battery.read_mv() if self.battery else None

    def timestamp(self):
        t = time.localtime()
        return "%04d-%02d-%02d %02d:%02d:%02d" % t[0:6]

    def log(self, mv, pct, action, note=""):
        batt = self.read_battery()
        line = "%s,%d,%d,%d,%s,%s,%s" % (self.timestamp(), int(time.time()), mv, pct,
                                        batt if batt is not None else "", action, note)
        print(line)
        store.append_log(line, self.cfg.get("log", {}).get("max_bytes", 200000))

    def measure(self):
        s = self.cfg["sensor"]
        mv = self.sensor.read_mv()
        return mv, logic.moisture_pct(mv, s["raw_dry_mv"], s["raw_wet_mv"])

    def valve_op(self, which):
        """弁を開く/閉じる。駆動中フラグを state に残し、駆動中のリセット（電圧降下）を検出できるようにする。"""
        self.state["valve_busy"] = which
        store.save_state(self.state)
        if which == "open":
            self.valve.open(self.feed)
        else:
            self.valve.close(self.feed)
        self.state["valve_busy"] = None
        store.save_state(self.state)

    def _use_light_sleep(self):
        # nc_mosfet は待機中も出力 H を保つ必要があるので light sleep しない
        # （ESP32-C3 は light sleep 中に GPIO がスリープ用設定へ切り替わる）
        return (self.cfg["watering"].get("light_sleep_wait", True)
                and not self.cfg.get("debug_no_sleep")
                and self.cfg["valve"].get("type") != "nc_mosfet")

    def _wait(self, sec):
        """sec 秒待つ。WDT を餌付けし、設定ボタン長押し(2秒)で中断（False を返す）。
        待機中は 1 秒ずつ light sleep して電流を下げる（散水中・休憩中とも弁は無通電で保持される）。"""
        held = 0
        light = self._use_light_sleep()
        for _ in range(int(sec)):
            self.feed()
            if light:
                machine.lightsleep(1000)
            else:
                time.sleep(1)
            held = held + 1 if self.button.pressed() else 0
            if held >= 2:
                return False
        return True

    def do_watering(self, manual=False):
        """設定どおりに 散水→休憩→散水… を実行する。どの経路でも最後に弁を閉じる。"""
        w = self.cfg["watering"]
        hard = w.get("max_spray_sec_hard_limit", 900)
        steps = logic.watering_steps(w)
        aborted = False
        try:
            for i, (kind, sec) in enumerate(steps):
                if kind == "spray":
                    sec = min(sec, hard)
                    self.led.on()
                    self.valve_op("open")
                    ok = self._wait(sec)
                    self.valve_op("close")
                    self.led.off()
                    if not ok:
                        aborted = True
                        self.log(-1, -1, "water_abort", "button")
                        break
                    if w.get("recheck_after_each_spray") and i < len(steps) - 1:
                        mv, pct = self.measure()
                        self.log(mv, pct, "recheck", "after spray %d" % (i // 2 + 1))
                        if pct >= self.cfg["threshold_off_pct"]:
                            self.log(mv, pct, "water_stop_wet", "")
                            break
                else:
                    if not self._wait(sec):
                        aborted = True
                        self.log(-1, -1, "water_abort", "button")
                        break
        finally:
            self.valve_op("close")
            self.valve.idle()
            self.led.off()
        logic.finish_watering(self.state, int(time.time()))
        self.state["last_action"] = "water_manual" if manual else ("water_abort" if aborted else "water_done")
        store.save_state(self.state)

    def run_cycle(self):
        now = int(time.time())
        mv, pct = self.measure()
        hour = time.localtime()[3] if self.clock_valid() else None
        batt = self.read_battery()
        action = logic.plan(self.cfg, self.state, now, pct, hour, batt)
        self.log(mv, pct, action)
        self.state["last_pct"] = pct
        self.state["last_action"] = action
        store.save_state(self.state)
        if action == logic.WATER:
            self.do_watering()


def go_to_sleep(ctx, minutes):
    ctx.valve.idle()
    ctx.led.off()
    ms = int(minutes * 60 * 1000)
    if ctx.cfg.get("debug_no_sleep"):
        print("debug_no_sleep: sleeping %d ms with time.sleep" % ms)
        t0 = time.ticks_ms()
        while time.ticks_diff(time.ticks_ms(), t0) < ms:
            ctx.feed()
            time.sleep(1)
        machine.reset()
    print("deep sleep %d ms" % ms)
    machine.deepsleep(ms)


def main():
    cfg = store.load_config()
    ctx = Context(cfg)
    cold_boot = machine.reset_cause() != machine.DEEPSLEEP_RESET
    if cold_boot:
        # 電源投入/リセット/ブラウンアウト復帰時は、散水途中だった可能性があるので必ず閉じる
        ctx.led.blink(3, 100, 100)
        busy = ctx.state.get("valve_busy")
        ctx.log(-1, -1, "boot", "reset_cause=%d" % machine.reset_cause())
        if busy:
            # 弁の駆動中にリセット＝電池の電圧降下（ブラウンアウト）の疑い。
            # すぐ再散水すると同じことを繰り返すので、散水間隔ぶん待たせる。
            ctx.log(-1, -1, "reset_in_valve_op", busy)
            ctx.state["last_water_end"] = int(time.time())
        ctx.valve_op("close")

    if ctx.button.pressed():
        ctx.log(-1, -1, "config_mode", "")
        import webcfg
        webcfg.run(ctx)
        cfg = ctx.cfg  # 設定モードで更新された可能性

    ctx.run_cycle()
    go_to_sleep(ctx, cfg["measure_interval_min"])


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.print_exception(e)
        try:
            store.append_log("%d,error,%s" % (time.time(), repr(e)))
        except Exception:
            pass
        # 例外で起き続けて電池を消耗しないよう、60秒寝て再試行
        machine.deepsleep(60 * 1000)
