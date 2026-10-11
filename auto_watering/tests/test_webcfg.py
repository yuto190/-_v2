"""webcfg.py の弁の設定欄（弁の種類ごとの表示と保存）を CPython で検証する。
python3 -m unittest auto_watering/tests/test_webcfg.py
"""
import os
import sys
import types
import unittest

for name in ("machine", "network"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["machine"].Pin = getattr(sys.modules["machine"], "Pin", object)
sys.modules["machine"].ADC = getattr(sys.modules["machine"], "ADC", object)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "firmware"))
import webcfg  # noqa: E402


class ValveFormTest(unittest.TestCase):
    def test_latch_shows_pulse_fields(self):
        html = "".join(webcfg.valve_form({"type": "latch_2wire", "pulse_ms": 100, "close_pulses": 2}))
        self.assertIn("ラッチ式電磁弁", html)
        self.assertIn("name='pulse_ms'", html)
        self.assertNotIn("travel_ms", html)

    def test_ball_shows_travel_field(self):
        html = "".join(webcfg.valve_form({"type": "ball_3wire", "travel_ms": 8000}))
        self.assertIn("電動ボールバルブ", html)
        self.assertIn("name='travel_ms'", html)
        self.assertIn("value='8000'", html)
        self.assertNotIn("pulse_ms", html)
        self.assertNotIn("ラッチ", html)
        self.assertIn("reverse_polarity", html)

    def test_nc_has_no_polarity(self):
        html = "".join(webcfg.valve_form({"type": "nc_mosfet"}))
        self.assertIn("常時閉", html)
        self.assertNotIn("reverse_polarity", html)

    def test_apply_ball(self):
        v = {"type": "ball_3wire", "travel_ms": 8000, "pulse_ms": 100}
        self.assertTrue(webcfg.apply_valve_form(v, {"valve_form": "ball_3wire", "travel_ms": "7500", "reverse_polarity": "on"}))
        self.assertEqual(v["travel_ms"], 7500)
        self.assertTrue(v["reverse_polarity"])
        self.assertEqual(v["pulse_ms"], 100)  # ラッチ用の値は触らない

    def test_apply_ball_clamps(self):
        v = {"type": "ball_3wire", "travel_ms": 8000}
        webcfg.apply_valve_form(v, {"valve_form": "ball_3wire", "travel_ms": "999999"})
        self.assertEqual(v["travel_ms"], webcfg.TRAVEL_MS_MAX)
        webcfg.apply_valve_form(v, {"valve_form": "ball_3wire", "travel_ms": "10"})
        self.assertEqual(v["travel_ms"], webcfg.TRAVEL_MS_MIN)
        self.assertFalse(v["reverse_polarity"])

    def test_apply_latch(self):
        v = {"type": "latch_2wire", "pulse_ms": 100, "close_pulses": 2, "travel_ms": 8000}
        webcfg.apply_valve_form(v, {"valve_form": "latch_2wire", "pulse_ms": "900", "close_pulses": "3"})
        self.assertEqual(v["pulse_ms"], 500)
        self.assertEqual(v["close_pulses"], 3)
        self.assertEqual(v["travel_ms"], 8000)

    def test_apply_ignores_stale_form(self):
        v = {"type": "ball_3wire", "travel_ms": 8000}
        self.assertFalse(webcfg.apply_valve_form(v, {"valve_form": "latch_2wire", "pulse_ms": "50"}))
        self.assertEqual(v, {"type": "ball_3wire", "travel_ms": 8000})
        self.assertFalse(webcfg.apply_valve_form(v, {}))


if __name__ == "__main__":
    unittest.main()
