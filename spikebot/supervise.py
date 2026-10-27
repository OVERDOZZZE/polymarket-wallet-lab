"""Keeps the bot running: restarts `python -m spikebot` 5 s after any exit, until logs/spikebot.STOP exists.

    nohup .venv/Scripts/python -m spikebot.supervise [--variant wide] > logs/spikebot_supervisor.out 2>&1 &"""
import argparse
import os
import subprocess
import sys
import time

from . import config as C


def say(msg):
    with open(C.SUPERVISOR_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}Z {msg}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="main", choices=sorted(C.VARIANTS))
    C.apply_variant(ap.parse_args().variant)
    C.LOG_DIR.mkdir(parents=True, exist_ok=True)
    C.STOP_PATH.unlink(missing_ok=True)
    C.SUPERVISOR_PID_PATH.write_text(str(os.getpid()))
    say(f"supervisor start pid {os.getpid()}")
    try:
        while not C.STOP_PATH.exists():
            t0 = time.time()
            p = subprocess.Popen([sys.executable, "-m", "spikebot", "--variant", C.VARIANT], cwd=str(C.ROOT),
                                 stdout=subprocess.DEVNULL, stderr=open(C.LOG_DIR / "spikebot_stderr.log", "a"))
            say(f"bot started pid {p.pid}")
            code = p.wait()
            say(f"bot exited code {code} after {time.time() - t0:.0f} s")
            if C.STOP_PATH.exists():
                break
            time.sleep(5)
    finally:
        say("supervisor stop")
        C.SUPERVISOR_PID_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
