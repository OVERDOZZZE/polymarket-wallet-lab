"""python -m backend  ->  start the server and open the app in the browser."""
import sys
import threading
import webbrowser

import uvicorn

from .config import HOST, PORT


def main():
    if "--no-browser" not in sys.argv:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    uvicorn.run("backend.api.app:app", host=HOST, port=PORT, log_level="warning", ws_ping_interval=20)


if __name__ == "__main__":
    main()
