"""
Wi-Fi 設定モード。

本体の設定ボタンを押したままリセットすると起動する。
ESP32 がアクセスポイント（SSID: config の wifi.ap_ssid）になり、
スマホ/PC から http://192.168.4.1/ を開くと
  - 現在の水分値・電池電圧・状態・直近ログ
  - しきい値、散水時間/休憩/繰り返し、測定間隔などの変更
  - 弁の開閉テスト、今すぐ散水、時計合わせ
ができる。一定時間操作がなければ自動で通常動作に戻る。
"""
import json
import socket
import time

import machine
import network

import hal
import logic
import store

_HTML_HEAD = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>散水コントローラー設定</title>
<style>body{font-family:sans-serif;max-width:640px;margin:1em auto;padding:0 1em}
label{display:block;margin:.6em 0 .2em}input,select{width:100%;padding:.4em;font-size:1em}
button{padding:.6em 1em;margin:.3em .2em .3em 0;font-size:1em}
pre{background:#f3f3f3;padding:.6em;overflow:auto;font-size:.8em}
.row{display:flex;gap:1em}.row>div{flex:1}.note{color:#666;font-size:.85em}</style></head><body>"""


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")


def _unquote(s):
    s = s.replace("+", " ")
    out = bytearray()
    i = 0
    b = s.encode()
    while i < len(b):
        c = b[i]
        if c == 0x25 and i + 2 < len(b):  # '%'
            try:
                out.append(int(b[i + 1:i + 3].decode(), 16))
                i += 3
                continue
            except ValueError:
                pass
        out.append(c)
        i += 1
    return out.decode()


def _parse_form(body):
    d = {}
    for pair in body.split("&"):
        if not pair:
            continue
        k, _, v = pair.partition("=")
        d[_unquote(k)] = _unquote(v)
    return d


def _weekday(y, m, d):
    """0=月曜 … 6=日曜（Sakamoto のアルゴリズム。MicroPython/CPython 両対応）"""
    t = (0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4)
    if m < 3:
        y -= 1
    w = (y + y // 4 - y // 100 + y // 400 + t[m - 1] + d) % 7  # 0=日曜
    return (w + 6) % 7


def _int(d, key, default):
    try:
        return int(float(d.get(key, default)))
    except (ValueError, TypeError):
        return default


class ConfigServer:
    def __init__(self, ctx):
        self.ctx = ctx          # main.Context
        self.cfg = ctx.cfg
        self.msg = ""
        self.running = True

    # ---------- ページ生成 ----------
    def page(self):
        c = self.cfg
        w = c["watering"]
        s = c["sensor"]
        st = self.ctx.state
        try:
            mv = self.ctx.sensor.read_mv()
            pct = logic.moisture_pct(mv, s["raw_dry_mv"], s["raw_wet_mv"])
        except Exception as e:  # noqa
            mv, pct = -1, -1
        batt = self.ctx.read_battery()
        hours = c.get("allowed_hours") or ["", ""]
        now = time.time()
        t = time.localtime()
        clock = "%04d-%02d-%02d %02d:%02d" % t[0:5] + ("" if self.ctx.clock_valid() else "（未設定）")

        def rel(ts):
            if ts is None:
                return "-"
            return "%d 分前" % ((now - ts) // 60)

        h = [_HTML_HEAD, "<h2>散水コントローラー</h2>"]
        if self.msg:
            h.append("<p style='color:#060'><b>%s</b></p>" % _esc(self.msg))
        h.append("<h3>現在の状態</h3><pre>")
        h.append("土壌水分: %d %%  (センサー %d mV)\n" % (pct, mv))
        h.append("電池: %s\n" % ("%d mV" % batt if batt is not None else "未測定"))
        h.append("時計: %s\n" % clock)
        h.append("乾燥検出から: %s\n" % rel(st.get("dry_since")))
        h.append("前回散水終了: %s\n" % rel(st.get("last_water_end")))
        h.append("本日の散水回数: %s\n" % st.get("waterings_today", 0))
        h.append("前回判定: %s\n" % st.get("last_action"))
        h.append("</pre>")

        h.append("<form method='post' action='/save'><h3>散水の設定</h3>")
        h.append("<label>散水開始しきい値 [%%]（これ未満で乾燥と判定）<input name='threshold_on_pct' type='number' value='%s'></label>" % c["threshold_on_pct"])
        h.append("<label>散水打ち切りしきい値 [%%]（散水途中の再測定でこれ以上なら中止）<input name='threshold_off_pct' type='number' value='%s'></label>" % c["threshold_off_pct"])
        h.append("<label>乾燥検出から散水開始までの待ち時間 [分]<input name='dry_confirm_delay_min' type='number' value='%s'></label>" % c["dry_confirm_delay_min"])
        h.append("<div class='row'><div><label>1回の散水時間 [秒]<input name='spray_sec' type='number' value='%s'></label></div>" % w["spray_sec"])
        h.append("<div><label>休憩 [秒]<input name='pause_sec' type='number' value='%s'></label></div>" % w["pause_sec"])
        h.append("<div><label>繰り返し回数<input name='repeats' type='number' value='%s'></label></div></div>" % w["repeats"])
        h.append("<label><input type='checkbox' name='recheck' style='width:auto' %s> 各散水後に再測定し、打ち切りしきい値以上なら残りを中止</label>" % ("checked" if w.get("recheck_after_each_spray") else ""))
        h.append("<label>前回散水終了から次の散水までの最短間隔 [分]<input name='min_interval_between_waterings_min' type='number' value='%s'></label>" % c["min_interval_between_waterings_min"])
        h.append("<label>1日の散水回数上限（0=無制限）<input name='max_waterings_per_day' type='number' value='%s'></label>" % c.get("max_waterings_per_day", 0))
        h.append("<div class='row'><div><label>散水許可 開始時 [0-23]（空=制限なし）<input name='hour_start' type='number' value='%s'></label></div>" % hours[0])
        h.append("<div><label>終了時 [0-23]<input name='hour_end' type='number' value='%s'></label></div></div>" % hours[1])
        h.append("<p class='note'>時間帯制限は時計が設定されているときだけ有効です。</p>")
        h.append("<h3>測定・センサー</h3>")
        h.append("<label>測定間隔 [分]<input name='measure_interval_min' type='number' value='%s'></label>" % c["measure_interval_min"])
        h.append("<div class='row'><div><label>乾燥時のセンサー値 [mV]（空気中）<input name='raw_dry_mv' type='number' value='%s'></label></div>" % s["raw_dry_mv"])
        h.append("<div><label>湿潤時のセンサー値 [mV]（水中）<input name='raw_wet_mv' type='number' value='%s'></label></div></div>" % s["raw_wet_mv"])
        v = c["valve"]
        h.append("<h3>弁（ラッチ式電磁弁）</h3>")
        h.append("<div class='row'><div><label>パルス幅 [ms]（10〜500）<input name='pulse_ms' type='number' value='%s'></label></div>" % v.get("pulse_ms", 100))
        h.append("<div><label>閉パルス回数<input name='close_pulses' type='number' value='%s'></label></div></div>" % v.get("close_pulses", 2))
        h.append("<label><input type='checkbox' name='reverse_polarity' style='width:auto' %s> 開閉が逆なので極性を入れ替える</label>" % ("checked" if v.get("reverse_polarity") else ""))
        h.append("<p class='note'>弁の種類: %s。保存するとすぐ下の「弁を開く/閉じる」に反映されます。</p>" % _esc(v.get("type", "")))
        h.append("<button type='submit'>保存</button></form>")

        h.append("<h3>テスト</h3><form method='post' action='/valve_open' style='display:inline'><button>弁を開く</button></form>")
        h.append("<form method='post' action='/valve_close' style='display:inline'><button>弁を閉じる</button></form>")
        h.append("<form method='post' action='/water_now' style='display:inline' onsubmit='return confirm(\"設定どおり1サイクル散水します\")'><button>今すぐ1サイクル散水</button></form>")
        h.append("<form method='post' action='/time' style='display:inline' id='tf'>")
        for k in ("y", "mo", "d", "h", "mi", "s"):
            h.append("<input type='hidden' name='%s' id='t_%s'>" % (k, k))
        h.append("<button type='submit'>この端末の時刻に合わせる</button></form>")
        h.append("<form method='post' action='/exit' style='display:inline'><button>設定モード終了</button></form>")
        h.append("""<script>document.getElementById('tf').onsubmit=function(){var n=new Date();
document.getElementById('t_y').value=n.getFullYear();document.getElementById('t_mo').value=n.getMonth()+1;
document.getElementById('t_d').value=n.getDate();document.getElementById('t_h').value=n.getHours();
document.getElementById('t_mi').value=n.getMinutes();document.getElementById('t_s').value=n.getSeconds();}</script>""")

        h.append("<h3>直近のログ</h3><pre>")
        h.append(_esc("\n".join(store.tail_log(30))))
        h.append("</pre><p><a href='/log'>ログ全体 (CSV)</a></p>")
        h.append("<h3>詳細設定 (JSON)</h3><form method='post' action='/save_json'><textarea name='json' rows='12' style='width:100%%;font-family:monospace'>%s</textarea><br><button>JSONを保存</button></form>" % _esc(json.dumps(c)))
        h.append("</body></html>")
        return "".join(h)

    # ---------- ハンドラ ----------
    def handle(self, method, path, body):
        c = self.cfg
        if method == "GET" and path == "/log":
            try:
                with open(store.LOG_FILE) as f:
                    data = f.read()
            except OSError:
                data = ""
            return "text/csv", data

        if method == "POST":
            form = _parse_form(body)
            if path == "/save":
                c["threshold_on_pct"] = _int(form, "threshold_on_pct", c["threshold_on_pct"])
                c["threshold_off_pct"] = _int(form, "threshold_off_pct", c["threshold_off_pct"])
                c["dry_confirm_delay_min"] = _int(form, "dry_confirm_delay_min", c["dry_confirm_delay_min"])
                c["min_interval_between_waterings_min"] = _int(form, "min_interval_between_waterings_min", c["min_interval_between_waterings_min"])
                c["max_waterings_per_day"] = _int(form, "max_waterings_per_day", 0)
                c["measure_interval_min"] = max(1, _int(form, "measure_interval_min", c["measure_interval_min"]))
                w = c["watering"]
                w["spray_sec"] = max(1, _int(form, "spray_sec", w["spray_sec"]))
                w["pause_sec"] = max(0, _int(form, "pause_sec", w["pause_sec"]))
                w["repeats"] = max(1, _int(form, "repeats", w["repeats"]))
                w["recheck_after_each_spray"] = "recheck" in form
                s = c["sensor"]
                s["raw_dry_mv"] = _int(form, "raw_dry_mv", s["raw_dry_mv"])
                s["raw_wet_mv"] = _int(form, "raw_wet_mv", s["raw_wet_mv"])
                hs, he = form.get("hour_start", ""), form.get("hour_end", "")
                c["allowed_hours"] = [int(hs), int(he)] if hs.strip() and he.strip() else None
                v = c["valve"]
                if "pulse_ms" in form:
                    v["pulse_ms"] = min(500, max(10, _int(form, "pulse_ms", v.get("pulse_ms", 100))))
                    v["close_pulses"] = min(5, max(1, _int(form, "close_pulses", v.get("close_pulses", 2))))
                    v["reverse_polarity"] = "reverse_polarity" in form
                    self.ctx.valve = hal.make_valve(c)
                store.save_config(c)
                self.msg = "保存しました"
            elif path == "/save_json":
                try:
                    new = json.loads(form.get("json", ""))
                    for k in ("pins", "valve", "sensor", "watering", "wifi"):
                        if k not in new:
                            raise ValueError("missing " + k)
                    c.clear()
                    c.update(new)
                    store.save_config(c)
                    self.msg = "JSONを保存しました（ピン/弁種別の変更はリセット後に有効）"
                except ValueError as e:
                    self.msg = "JSONエラー: %s" % e
            elif path == "/valve_open":
                self.ctx.valve_op("open")
                self.msg = "弁を開きました（閉じ忘れ注意。設定モード終了時に自動で閉じます）"
            elif path == "/valve_close":
                self.ctx.valve_op("close")
                self.msg = "弁を閉じました"
            elif path == "/water_now":
                self.ctx.do_watering(manual=True)
                self.msg = "散水サイクルを実行しました"
            elif path == "/time":
                self.set_time(form)
                self.msg = "時計を合わせました"
            elif path == "/exit":
                self.running = False
                self.msg = "終了します"
        return "text/html", self.page()

    def set_time(self, form):
        y, mo, d = _int(form, "y", 2000), _int(form, "mo", 1), _int(form, "d", 1)
        h, mi, s = _int(form, "h", 0), _int(form, "mi", 0), _int(form, "s", 0)
        old = int(time.time())
        machine.RTC().datetime((y, mo, d, _weekday(y, mo, d), h, mi, s, 0))
        delta = int(time.time()) - old
        # 保存済みの相対時刻も同じだけずらす（時計合わせで「乾燥から何分」が狂わないように）
        st = self.ctx.state
        for k in ("dry_since", "last_water_end"):
            if st.get(k) is not None:
                st[k] += delta
        st["day_key"] = int(time.time()) // 86400
        store.save_state(st)

    # ---------- サーバー ----------
    def serve(self, timeout_min):
        wcfg = self.cfg["wifi"]
        ap = network.WLAN(network.AP_IF)
        ap.active(True)
        ap.config(essid=wcfg["ap_ssid"], password=wcfg["ap_password"],
                  authmode=network.AUTH_WPA_WPA2_PSK)
        while not ap.active():
            time.sleep_ms(100)
        print("AP:", ap.ifconfig())

        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", 80))
        srv.listen(2)
        srv.settimeout(1)
        last = time.time()
        try:
            while self.running and time.time() - last < timeout_min * 60:
                self.ctx.feed()
                self.ctx.led.blink(1, 20, 0)
                try:
                    cl, _ = srv.accept()
                except OSError:
                    continue
                last = time.time()
                try:
                    cl.settimeout(5)
                    self._handle_client(cl)
                except Exception as e:  # noqa
                    print("http error:", e)
                finally:
                    cl.close()
        finally:
            srv.close()
            ap.active(False)

    def _handle_client(self, cl):
        f = cl.makefile("rwb", 0)
        line = f.readline()
        if not line:
            return
        parts = line.decode().split()
        if len(parts) < 2:
            return
        method, path = parts[0], parts[1].split("?")[0]
        length = 0
        while True:
            hdr = f.readline()
            if not hdr or hdr == b"\r\n":
                break
            if hdr.lower().startswith(b"content-length:"):
                length = int(hdr.split(b":")[1].strip())
        body = f.read(length).decode() if length else ""
        ctype, resp = self.handle(method, path, body)
        cl.write(("HTTP/1.0 200 OK\r\nContent-Type: %s; charset=utf-8\r\nConnection: close\r\n\r\n" % ctype).encode())
        cl.write(resp.encode())


def run(ctx):
    srv = ConfigServer(ctx)
    srv.serve(ctx.cfg["wifi"].get("config_mode_timeout_min", 10))
    ctx.valve_op("close")   # テストで開けたままでも必ず閉じる
