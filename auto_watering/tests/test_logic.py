"""CPython で動く散水ロジックの単体テスト。  python3 -m unittest auto_watering/tests/test_logic.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "firmware"))
import logic  # noqa: E402

CFG = {
    "threshold_on_pct": 35,
    "dry_confirm_delay_min": 60,
    "min_interval_between_waterings_min": 360,
    "max_waterings_per_day": 2,
    "allowed_hours": None,
    "low_battery_mv": 3400,
}
MIN = 60


def fresh():
    return {"dry_since": None, "last_water_end": None, "day_key": None, "waterings_today": 0}


class MoistureTest(unittest.TestCase):
    def test_linear_and_clamp(self):
        self.assertEqual(logic.moisture_pct(2200, 2200, 1200), 0)
        self.assertEqual(logic.moisture_pct(1200, 2200, 1200), 100)
        self.assertEqual(logic.moisture_pct(1700, 2200, 1200), 50)
        self.assertEqual(logic.moisture_pct(2500, 2200, 1200), 0)
        self.assertEqual(logic.moisture_pct(900, 2200, 1200), 100)
        self.assertEqual(logic.moisture_pct(1500, 1500, 1500), 0)


class PlanTest(unittest.TestCase):
    def test_wet_resets_dry_since(self):
        st = fresh()
        st["dry_since"] = 100
        self.assertEqual(logic.plan(CFG, st, 1000, 60), logic.WET)
        self.assertIsNone(st["dry_since"])

    def test_confirm_delay_then_water(self):
        st = fresh()
        t = 10_000
        self.assertEqual(logic.plan(CFG, st, t, 20), logic.DRY_WAIT)
        self.assertEqual(st["dry_since"], t)
        # 59分後: まだ待つ
        self.assertEqual(logic.plan(CFG, st, t + 59 * MIN, 20), logic.DRY_WAIT)
        # 60分後: 散水
        self.assertEqual(logic.plan(CFG, st, t + 60 * MIN, 20), logic.WATER)

    def test_rewet_during_delay_cancels(self):
        st = fresh()
        t = 10_000
        logic.plan(CFG, st, t, 20)
        self.assertEqual(logic.plan(CFG, st, t + 30 * MIN, 50), logic.WET)  # 雨が降った
        self.assertIsNone(st["dry_since"])
        # 再度乾いたら待ち時間はやり直し
        self.assertEqual(logic.plan(CFG, st, t + 40 * MIN, 20), logic.DRY_WAIT)
        self.assertEqual(logic.plan(CFG, st, t + 99 * MIN, 20), logic.DRY_WAIT)
        self.assertEqual(logic.plan(CFG, st, t + 100 * MIN, 20), logic.WATER)

    def test_cooldown_after_watering(self):
        st = fresh()
        t = 10_000
        logic.plan(CFG, st, t, 20)
        self.assertEqual(logic.plan(CFG, st, t + 60 * MIN, 20), logic.WATER)
        logic.finish_watering(st, t + 70 * MIN)
        self.assertIsNone(st["dry_since"])
        self.assertEqual(st["waterings_today"], 1)
        # 散水後も乾いたまま → dry_since が新たに立ち、確認待ち
        t2 = t + 80 * MIN
        self.assertEqual(logic.plan(CFG, st, t2, 20), logic.DRY_WAIT)
        # 確認待ちは過ぎたが、前回散水から6時間未満 → cooldown
        self.assertEqual(logic.plan(CFG, st, t2 + 60 * MIN, 20), logic.COOLDOWN)
        self.assertEqual(logic.plan(CFG, st, t + 70 * MIN + 360 * MIN, 20), logic.WATER)

    def test_daily_limit(self):
        st = fresh()
        cfg = dict(CFG, min_interval_between_waterings_min=0, dry_confirm_delay_min=0)
        day0 = 86400 * 10
        t = day0
        for _ in range(2):
            logic.plan(cfg, st, t, 20)                # dry_since 設定
            self.assertEqual(logic.plan(cfg, st, t + 1, 20), logic.WATER)
            logic.finish_watering(st, t + 2)
            t += 10
        logic.plan(cfg, st, t, 20)
        self.assertEqual(logic.plan(cfg, st, t + 1, 20), logic.DAILY_LIMIT)
        # 翌日はリセット
        d1 = day0 + 86400
        logic.plan(cfg, st, d1, 20)
        self.assertEqual(logic.plan(cfg, st, d1 + 1, 20), logic.WATER)

    def test_allowed_hours(self):
        st = fresh()
        cfg = dict(CFG, dry_confirm_delay_min=0, allowed_hours=[5, 10])
        logic.plan(cfg, st, 1000, 20)
        self.assertEqual(logic.plan(cfg, st, 1001, 20, hour=13), logic.OUTSIDE_HOURS)
        self.assertEqual(logic.plan(cfg, st, 1001, 20, hour=6), logic.WATER)
        # 時計が未設定（hour=None）なら時間帯制限は無視
        self.assertEqual(logic.plan(cfg, st, 1001, 20, hour=None), logic.WATER)
        # 日跨ぎ [22, 4)
        cfg2 = dict(cfg, allowed_hours=[22, 4])
        self.assertEqual(logic.plan(cfg2, st, 1001, 20, hour=23), logic.WATER)
        self.assertEqual(logic.plan(cfg2, st, 1001, 20, hour=3), logic.WATER)
        self.assertEqual(logic.plan(cfg2, st, 1001, 20, hour=12), logic.OUTSIDE_HOURS)

    def test_clock_reset_does_not_block_forever(self):
        # 2 か月動いた後に電池交換 → RTC が 0 付近に戻った
        st = fresh()
        st["last_water_end"] = 60 * 86400
        st["dry_since"] = 60 * 86400 + 3600
        t = 100
        self.assertEqual(logic.plan(CFG, st, t, 10), logic.DRY_WAIT)
        self.assertEqual(st["dry_since"], t)
        self.assertEqual(st["last_water_end"], t)
        # 補正後は通常どおり min_interval（6時間）後に散水できる
        t2 = t + 360 * MIN
        self.assertEqual(logic.plan(CFG, st, t2, 10), logic.WATER)

    def test_low_battery(self):
        st = fresh()
        cfg = dict(CFG, dry_confirm_delay_min=0)
        logic.plan(cfg, st, 1000, 20)
        self.assertEqual(logic.plan(cfg, st, 1001, 20, batt_mv=3300), logic.LOW_BATTERY)
        self.assertEqual(logic.plan(cfg, st, 1001, 20, batt_mv=3900), logic.WATER)
        self.assertEqual(logic.plan(cfg, st, 1001, 20, batt_mv=None), logic.WATER)


class StepsTest(unittest.TestCase):
    def test_pattern(self):
        self.assertEqual(
            logic.watering_steps({"spray_sec": 180, "pause_sec": 300, "repeats": 2}),
            [("spray", 180), ("pause", 300), ("spray", 180)],
        )
        self.assertEqual(logic.watering_steps({"spray_sec": 60, "pause_sec": 0, "repeats": 3}),
                         [("spray", 60), ("spray", 60), ("spray", 60)])
        self.assertEqual(logic.watering_steps({"spray_sec": 90, "pause_sec": 100, "repeats": 1}),
                         [("spray", 90)])
        self.assertEqual(logic.watering_steps({"spray_sec": 90, "repeats": 0}), [("spray", 90)])


if __name__ == "__main__":
    unittest.main()
