# boot.py: main.py の前に実行される。ここでは何もしない（デバッグ時に REPL へ落ちやすくするため）。
import gc
gc.collect()
