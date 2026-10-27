"""python -m spikebot.stop : asks the bot and its supervisor to exit (bot notices within 5 s), then waits."""
import argparse
import time

from . import config as C


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="main", choices=sorted(C.VARIANTS))
    C.apply_variant(ap.parse_args().variant)
    C.LOG_DIR.mkdir(exist_ok=True)
    C.STOP_PATH.write_text("stop")
    for _ in range(30):
        if not C.PID_PATH.exists() and not C.SUPERVISOR_PID_PATH.exists():
            print("stopped")
            return
        time.sleep(1)
    print("still running after 30 s: check logs/spikebot.pid and logs/spikebot_supervisor.pid")


if __name__ == "__main__":
    main()
