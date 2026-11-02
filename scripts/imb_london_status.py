"""One status line of a live imb_maker bot and its paper twin (read-only, from data/bots/<id>/state.json):

    python3 -m scripts.imb_london_status [bot id]        (default imb-london; exit code 3 = needs attention)

Attention: unit not running, halted, live realized <= -5, rejections other than not_enough_balance, stale state.
"""
import json
import subprocess
import sys
import time

bot = sys.argv[1] if len(sys.argv) > 1 else "imb-london"
s = json.load(open(f"data/bots/{bot}/state.json"))
L = s["live"]
active = subprocess.run(["systemctl", "is-active", f"pmtool-bot@{bot}"], capture_output=True, text=True).stdout.strip()
age = time.time() - s["ts"]
print(f"{time.strftime('%H:%M:%S', time.gmtime())} {bot} {active} state {age:.0f}s old | LIVE win {L['windows_done']} "
      f"won {L['windows_won']} realized {L['realized']:+.2f} rebate {L['rebate']:.2f} open {L['open_cost']:.2f} room "
      f"{L['room']:.2f} halted {L['halted']} fills {L['fills']} sh {L['fill_shares']:.0f} sent {L['sent']} rej "
      f"{L['rejected']} {L['rej']} usdc {L['usdc']} post {L['post_ms']} cancel {L['cancel_ms']} hb {L['counts'].get('hb_ok', 0)} | TWIN win "
      f"{s['windows_done']} pnl {s['trading_pnl']:+.2f} fills {len(s['fills'])} markout {s['markout_avg']} | errors "
      f"{len(s['errors'])}")
other_rej = sum(v for k, v in L["rej"].items() if k != "not_enough_balance")     # balance: the wallet is small
bad = active != "active" or (L["halted"] and L["open_cost"] <= 0) or L["realized"] <= -5 or other_rej > 0 or age > 60
sys.exit(3 if bad else 0)
