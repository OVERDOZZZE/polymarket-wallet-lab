"""One status line of the live fast player and its twin (read-only, state.json). Exit 3 = attention (unit down,
halted, stale state, live loss room <= $2)."""
import json
import subprocess
import sys
import time

bot = sys.argv[1] if len(sys.argv) > 1 else "fast-london"
s = json.load(open(f"data/bots/{bot}/state.json"))
L = s.get("live") or {}
active = subprocess.run(["systemctl", "is-active", f"pmtool-bot@{bot}"], capture_output=True, text=True).stdout.strip()
age = time.time() - s["ts"]
sent = L.get("sent") or 0
print(f"{time.strftime('%H:%M:%S', time.gmtime())} {bot} {active} {age:.0f}s | LIVE sent {sent} filled {L.get('filled')} "
      f"missed {L.get('missed')} rejected {L.get('rejected')} skipped {L.get('skipped')} fill% "
      f"{100 * (L.get('filled') or 0) / sent if sent else 0:.0f} spent {L.get('spent')} realized {L.get('realized')} "
      f"at_risk {L.get('at_risk')} room {L.get('room')} usdc {L.get('usdc')} order_ms {L.get('order_ms')} | TWIN "
      f"windows {s.get('windows_done')} pnl {s.get('trading_pnl')} | errors {len(s.get('errors') or [])}")
bad = active != "active" or L.get("halted") or age > 120 or (L.get("room") is not None and L["room"] <= 2.0)
sys.exit(3 if bad else 0)
