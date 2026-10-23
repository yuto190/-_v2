"""
XIAO の USB シリアル出力を、PC の時刻付きでファイルに記録する（ブラウンアウト調査用）。

    pip install pyserial
    python serial_log.py COM5            # Windows（ポート名はデバイスマネージャーで確認）
    python serial_log.py /dev/tty.usbmodem101   # macOS

- XIAO がリセットすると USB が切れるので「--- 切断 ---」を記録し、0.2 秒ごとに再接続を試みる。
  切断の直前の行（hb / http / valve_start など）が、落ちたときに何をしていたかを示す。
- ポートを開くときに DTR/RTS を立てない（ESP32-C3 の USB はこの信号でリセットされることがある）。
- mpremote や Thonny とは同時に使えない（ポートの取り合いになる）。
"""
import sys
import time

import serial


def stamp():
    t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t)) + ".%03d" % int((t % 1) * 1000)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    port = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else time.strftime("xiao_%Y%m%d_%H%M%S.log")
    with open(out, "a", encoding="utf-8") as f:
        def emit(line):
            line = "%s %s" % (stamp(), line)
            print(line)
            f.write(line + "\n")
            f.flush()

        emit("--- 記録開始 %s → %s ---" % (port, out))
        connected = False
        while True:
            try:
                s = serial.Serial()
                s.port = port
                s.baudrate = 115200
                s.timeout = 0.5
                s.dtr = False
                s.rts = False
                s.open()
            except (serial.SerialException, OSError):
                if connected:
                    emit("--- 切断 ---")
                    connected = False
                time.sleep(0.2)
                continue
            emit("--- 接続 ---")
            connected = True
            buf = b""
            try:
                while True:
                    buf += s.read(256)
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        emit(line.decode("utf-8", "replace").rstrip("\r"))
            except (serial.SerialException, OSError):
                if buf:
                    emit(buf.decode("utf-8", "replace"))
                emit("--- 切断 ---")
                connected = False
            except KeyboardInterrupt:
                emit("--- 記録終了 ---")
                return
            finally:
                s.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
