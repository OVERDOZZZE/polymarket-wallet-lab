"""python -m spikebot [--variant main|wide]   (one bot process; `python -m spikebot.supervise` keeps it running)"""
import argparse
import asyncio
import logging
import os
from logging.handlers import RotatingFileHandler

from . import config as C
from .bot import Bot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="main", choices=sorted(C.VARIANTS))
    C.apply_variant(ap.parse_args().variant)
    C.LOG_DIR.mkdir(parents=True, exist_ok=True)
    h = RotatingFileHandler(C.LOG_PATH, maxBytes=20_000_000, backupCount=5, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(h)
    for name in ("websockets", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    C.PID_PATH.write_text(str(os.getpid()))
    try:
        asyncio.run(Bot().run())
    finally:
        C.PID_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
