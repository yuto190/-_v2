"""webcfg.py の弁の設定欄（弁の種類ごとの表示と保存）とセンサー測定（/sensor）を CPython で検証する。
python3 -m unittest auto_watering/tests/test_webcfg.py
"""
import os
import sys
import json
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


class _FakeSensor:
    def __init__(self, mv=1200, fail=False):
        self.warmup_ms = 500
        self.mv = mv
        self.fail = fail
        self.used_warmup = None

    def read_mv(self):
        self.used_warmup = self.warmup_ms
        if self.fail:
            raise OSError("adc")
        return self.mv


class _FakeCtx:
    def __init__(self, sensor):
        self.cfg = {"sensor": {"raw_dry_mv": 2400, "raw_wet_mv": 0, "warmup_ms": 500}}
        self.sensor = sensor
        self.state = {}

    def read_battery(self):
        return 5200


class SensorEndpointTest(unittest.TestCase):
    def get(self, sensor, query=""):
        srv = webcfg.ConfigServer(_FakeCtx(sensor))
        ctype, body = srv.handle("GET", "/sensor", "", query)
        self.assertEqual(ctype, "application/json")
        return json.loads(body)

    def test_reading_and_pct(self):
        r = self.get(_FakeSensor(1200))
        self.assertEqual(r, {"mv": 1200, "pct": 50, "batt": 5200, "warmup": 500})

    def test_warmup_override_is_temporary_and_clamped(self):
        sen = _FakeSensor()
        self.assertEqual(self.get(sen, "w=2000")["warmup"], 2000)
        self.assertEqual(sen.used_warmup, 2000)
        self.assertEqual(sen.warmup_ms, 500)
        self.assertEqual(self.get(sen, "w=999999")["warmup"], webcfg.WARMUP_MS_MAX)
        self.assertEqual(self.get(sen, "w=-5")["warmup"], 0)
        self.assertEqual(self.get(sen, "w=abc")["warmup"], 500)

    def test_error_restores_warmup(self):
        sen = _FakeSensor(fail=True)
        self.assertIn("error", self.get(sen, "w=100"))
        self.assertEqual(sen.warmup_ms, 500)

    def test_panel_buttons_do_not_submit(self):
        html = "".join(webcfg.sensor_panel(500))
        self.assertEqual(html.count("<button"), html.count("type='button'"))
        self.assertIn("value='500'", html)


if __name__ == "__main__":
    unittest.main()
