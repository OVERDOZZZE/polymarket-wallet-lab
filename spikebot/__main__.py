"""python -m spikebot   (one bot process; use `python -m spikebot.supervise` to keep it running)"""
import asyncio
import logging
import os
from logging.handlers import RotatingFileHandler

from . import config as C
from .bot import Bot


def main():
    C.LOG_DIR.mkdir(exist_ok=True)
    h = RotatingFileHandler(C.LOG_PATH, maxBytes=20_000_000, backupCount=5, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(h)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    C.PID_PATH.write_text(str(os.getpid()))
    try:
        asyncio.run(Bot().run())
    finally:
        C.PID_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
