"""hal.py の弁駆動シーケンスを、machine をモックして CPython で検証する。
python3 -m unittest discover -s auto_watering/tests
"""
import os
import sys
import time
import types
import unittest

EVENTS = []


class _Pin:
    OUT = 1
    IN = 0
    PULL_UP = 2

    def __init__(self, n, mode=None, pull=None, value=0):
        self.n = n
        self.v = value
        EVENTS.append(("init", n, value))

    def value(self, v=None):
        if v is None:
            return self.v
        self.v = v
        EVENTS.append(("set", self.n, v))


class _ADC:
    ATTN_11DB = 3

    def __init__(self, pin, atten=None):
        pass

    def read_uv(self):
        return 0


_machine = types.ModuleType("machine")
_machine.Pin = _Pin
_machine.ADC = _ADC
sys.modules["machine"] = _machine
if not hasattr(time, "sleep_ms"):
    time.sleep_ms = lambda ms: EVENTS.append(("sleep", ms))

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "firmware"))
import hal  # noqa: E402

A, B, EN = 6, 7, 10


def cfg(vtype, en=EN):
    return {"valve": {"type": vtype, "travel_ms": 1000, "pulse_ms": 50, "power_settle_ms": 100},
            "pins": {"valve_a": A, "valve_b": B, "valve_power_en": en}}


def sets():
    return [e[1:] for e in EVENTS if e[0] == "set"]


class ValvePowerTest(unittest.TestCase):
    def setUp(self):
        EVENTS.clear()

    def test_ball_open_close_gates_boost(self):
        v = hal.make_valve(cfg("ball_3wire"))
        for e in EVENTS:  # 初期化時は全ピン L
            self.assertEqual(e[2], 0)
        EVENTS.clear()
        v.open()
        # EN=H → 安定待ち → A=H,B=L → 通電 → A=L,B=L → EN=L
        self.assertEqual(sets(), [(EN, 1), (A, 1), (B, 0), (A, 0), (B, 0), (EN, 0)])
        self.assertIn(("sleep", 100), EVENTS)
        self.assertLess(EVENTS.index(("sleep", 100)), EVENTS.index(("set", A, 1)))
        EVENTS.clear()
        v.close()
        self.assertEqual(sets(), [(EN, 1), (A, 0), (B, 1), (A, 0), (B, 0), (EN, 0)])

    def test_power_off_even_if_feed_raises(self):
        v = hal.make_valve(cfg("ball_3wire"))
        EVENTS.clear()

        def boom():
            raise RuntimeError("wdt")

        with self.assertRaises(RuntimeError):
            v.open(boom)
        self.assertEqual(sets()[-3:], [(A, 0), (B, 0), (EN, 0)])

    def test_no_power_pin(self):
        v = hal.make_valve(cfg("ball_3wire", en=None))
        EVENTS.clear()
        v.open()
        self.assertEqual(sets(), [(A, 1), (B, 0), (A, 0), (B, 0)])

    def test_latch(self):
        v = hal.make_valve(cfg("latch_2wire"))
        EVENTS.clear()
        v.close()
        self.assertEqual(sets(), [(EN, 1), (A, 0), (B, 1), (A, 0), (B, 0), (EN, 0)])

    def test_nc_keeps_power_while_open(self):
        v = hal.make_valve(cfg("nc_mosfet"))
        EVENTS.clear()
        v.open()
        self.assertEqual(sets(), [(EN, 1), (A, 1)])
        EVENTS.clear()
        v.idle()
        self.assertEqual(sets(), [(A, 0), (EN, 0)])


if __name__ == "__main__":
    unittest.main()
