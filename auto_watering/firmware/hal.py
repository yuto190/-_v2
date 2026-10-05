"""
ハードウェア抽象層（MicroPython / ESP32-C3 向け）。

- Sensor      : 静電容量式土壌水分センサー（アナログ）。測定時だけ GPIO から給電する
- Valve*      : 弁の種類ごとのドライバ
    ball_3wire : 電動ボールバルブ CR-02 型（COM=GND, OPEN線に+で開, CLOSE線に+で閉）
                 または CR-01 型（2線・極性反転）。Hブリッジ（TB67H450 / DRV8835）で駆動
    latch_2wire: ラッチ式（自己保持）電磁弁（本設計の標準: US Solid JFYSV10011-G, DC12V 15W）。
                 極性反転パルスで開閉。TB67H450 OUT1/OUT2 に2本をつなぐ
    nc_mosfet  : 常時閉(NC)電磁弁。Nch MOSFET で通電中だけ開く
  pins.valve_power_en を指定すると、弁を動かす間だけ昇圧DCDC（例: 12V）の EN を H にする。
- Button / Led / Battery
"""
import time
from machine import Pin, ADC


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) // 2


class Sensor:
    def __init__(self, adc_gpio, power_gpio, samples=16, warmup_ms=500):
        self.adc = ADC(Pin(adc_gpio), atten=ADC.ATTN_11DB)
        self.power = Pin(power_gpio, Pin.OUT, value=0)
        self.samples = samples
        self.warmup_ms = warmup_ms

    def read_mv(self):
        """センサーに給電 → 安定待ち → 複数回読んで中央値(mV) → 給電停止。"""
        self.power.value(1)
        time.sleep_ms(self.warmup_ms)
        vals = []
        for _ in range(self.samples):
            vals.append(self.adc.read_uv() // 1000)
            time.sleep_ms(5)
        self.power.value(0)
        return _median(vals)


class _PowerGate:
    """弁用電源（昇圧DCDC の EN）の ON/OFF。gpio が None なら何もしない。"""

    def __init__(self, gpio=None, settle_ms=0):
        self.pin = Pin(gpio, Pin.OUT, value=0) if gpio is not None else None
        self.settle_ms = settle_ms

    def on(self):
        if self.pin is not None:
            self.pin.value(1)
            if self.settle_ms:
                time.sleep_ms(self.settle_ms)

    def off(self):
        if self.pin is not None:
            self.pin.value(0)


class _TwoPin:
    def __init__(self, a_gpio, b_gpio, power=None):
        self.a = Pin(a_gpio, Pin.OUT, value=0)
        self.b = Pin(b_gpio, Pin.OUT, value=0)
        self.power = power or _PowerGate()

    def _drive(self, a, b, ms, feed=None):
        self.power.on()
        try:
            self.a.value(a)
            self.b.value(b)
            remaining = ms
            while remaining > 0:
                step = 200 if remaining > 200 else remaining
                time.sleep_ms(step)
                remaining -= step
                if feed:
                    feed()
        finally:
            self.a.value(0)
            self.b.value(0)
            self.power.off()

    def idle(self):
        self.a.value(0)
        self.b.value(0)
        self.power.off()


class ValveBall3Wire(_TwoPin):
    """CR-02 / CR-01 型電動ボールバルブ。内蔵リミットスイッチで止まるので travel_ms 通電後に出力を落とす。"""

    def __init__(self, a_gpio, b_gpio, travel_ms=8000, power=None):
        super().__init__(a_gpio, b_gpio, power)
        self.travel_ms = travel_ms

    def open(self, feed=None):
        self._drive(1, 0, self.travel_ms, feed)   # AOUT1=H → 赤 +

    def close(self, feed=None):
        self._drive(0, 1, self.travel_ms, feed)   # AOUT2=H → 青 +


PULSE_MS_MIN = 10
PULSE_MS_MAX = 500   # 15W コイルの発熱防止。ラッチ式は連続通電を想定していない


class ValveLatch2Wire(_TwoPin):
    """ラッチ式電磁弁。短いパルスで開、逆極性パルスで閉。

    開閉表示が無いので、閉じるときは close_pulses 回（間隔 pulse_gap_ms）パルスを出して確実にする。
    昇圧は1回の open/close の間だけ ON（複数パルスでも ON/OFF は1回）。
    """

    def __init__(self, a_gpio, b_gpio, pulse_ms=100, power=None, close_pulses=1, pulse_gap_ms=300):
        super().__init__(a_gpio, b_gpio, power)
        self.pulse_ms = min(max(int(pulse_ms), PULSE_MS_MIN), PULSE_MS_MAX)
        self.close_pulses = max(1, int(close_pulses))
        self.pulse_gap_ms = pulse_gap_ms

    def _pulses(self, a, b, n, feed):
        self.power.on()
        try:
            for i in range(n):
                if i:
                    time.sleep_ms(self.pulse_gap_ms)
                    if feed:
                        feed()
                self.a.value(a)
                self.b.value(b)
                time.sleep_ms(self.pulse_ms)
                self.a.value(0)
                self.b.value(0)
        finally:
            self.a.value(0)
            self.b.value(0)
            self.power.off()

    def open(self, feed=None):
        self._pulses(1, 0, 1, feed)

    def close(self, feed=None):
        self._pulses(0, 1, self.close_pulses, feed)


class ValveNC:
    """常時閉電磁弁。MOSFET ゲートを H にしている間だけ開く（通電し続ける）。"""

    def __init__(self, a_gpio, b_gpio=None, power=None):
        self.a = Pin(a_gpio, Pin.OUT, value=0)
        if b_gpio is not None:
            Pin(b_gpio, Pin.OUT, value=0)
        self.power = power or _PowerGate()

    def open(self, feed=None):
        self.power.on()
        self.a.value(1)

    def close(self, feed=None):
        self.a.value(0)
        self.power.off()

    def idle(self):
        self.close()


def make_valve(cfg):
    v = cfg["valve"]
    p = cfg["pins"]
    t = v.get("type", "latch_2wire")
    power = _PowerGate(p.get("valve_power_en"), v.get("power_settle_ms", 100))
    a, b = p["valve_a"], p["valve_b"]
    if v.get("reverse_polarity") and t != "nc_mosfet":
        a, b = b, a   # 開閉が逆のとき、配線を差し替えずに設定で入れ替える
    if t == "ball_3wire":
        return ValveBall3Wire(a, b, v.get("travel_ms", 8000), power)
    if t == "latch_2wire":
        return ValveLatch2Wire(a, b, v.get("pulse_ms", 100), power,
                               v.get("close_pulses", 2), v.get("pulse_gap_ms", 300))
    if t == "nc_mosfet":
        return ValveNC(p["valve_a"], p.get("valve_b"), power)
    raise ValueError("unknown valve type: %s" % t)


class Button:
    def __init__(self, gpio):
        self.pin = Pin(gpio, Pin.IN, Pin.PULL_UP)

    def pressed(self):
        return self.pin.value() == 0


class Led:
    def __init__(self, gpio):
        self.pin = Pin(gpio, Pin.OUT, value=0)

    def on(self):
        self.pin.value(1)

    def off(self):
        self.pin.value(0)

    def blink(self, n=1, on_ms=100, off_ms=100):
        for _ in range(n):
            self.pin.value(1)
            time.sleep_ms(on_ms)
            self.pin.value(0)
            time.sleep_ms(off_ms)


class Battery:
    """電池＋ → R1 → ADC → R2 → GND の分圧で電池電圧を読む。ratio = (R1+R2)/R2（単3×4 なら 3.0）。"""

    def __init__(self, adc_gpio, ratio=2.0):
        self.adc = ADC(Pin(adc_gpio), atten=ADC.ATTN_11DB)
        self.ratio = ratio

    def read_mv(self):
        vals = [self.adc.read_uv() // 1000 for _ in range(8)]
        return int(_median(vals) * self.ratio)
