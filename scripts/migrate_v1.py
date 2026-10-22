"""One-time migration from the v1 layout (Streamlit app, profiles/, bot_data/, cache/) to v2.

  * stops the running v1 paper bot
  * moves v1 code + raw files to archive/v1-<date>/ (nothing is deleted)
  * traders: profiles/*/profile.json -> DB `traders`; activity.csv -> activity.parquet;
    analysis CSVs copied, buys.csv -> buys.parquet; live/events-*.jsonl -> DB `trader_events`
  * cache/ -> data/cache/
  * bot_data/ -> bot "crouch-mm" in the DB; each archived reset becomes an older paper account

Run once:  .venv\\Scripts\\python scripts\\migrate_v1.py
"""
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARCH = ROOT / "archive" / f"v1-{time.strftime('%Y-%m-%d')}"


def stop_v1_bot():
    bd = ROOT / "bot_data"
    try:
        pid = int((bd / "bot.pid").read_text().strip())
    except (OSError, ValueError):
        return
    sys.path.insert(0, str(ROOT))
    from backend.util import kill_pid, pid_alive          # util has no side effects on import
    if not pid_alive(pid):
        return
    (bd / "control.json").write_text(json.dumps({"id": time.time_ns(), "action": "stop"}))
    for _ in range(40):
        if not pid_alive(pid):
            break
        time.sleep(0.25)
    kill_pid(pid)
    time.sleep(1.0)
    print(f"stopped v1 bot (pid {pid})")


def move(src, dst):
    src, dst = ROOT / src, dst
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        print(f"moved {src.name} -> {dst.relative_to(ROOT)}")


def jsonl(path):
    out = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def main():
    if (ROOT / "archive").exists() and any((ROOT / "archive").glob("v1-*")) and not (ROOT / "profiles").exists():
        sys.exit("already migrated")
    stop_v1_bot()
    # 1. v1 code and raw folders out of the way BEFORE backend.config creates data/
    for name in ("pmtool", "ui", "app.py", "pm_analyze.py", "legacy", ".streamlit", "README.md", "requirements.txt",
                 "run_app.bat"):
        move(name, ARCH / "code" / name)
    move("data", ARCH / "legacy-data")          # the original single-profile script's output
    move("profiles", ARCH / "profiles")
    move("bot_data", ARCH / "bot_data")
    (ROOT / "data").mkdir(exist_ok=True)
    move("cache", ROOT / "data" / "cache")

    sys.path.insert(0, str(ROOT))
    import pandas as pd
    from backend import db
    from backend.traders import store
    from backend.traders.analysis import BUY_COLS

    # 2. traders
    for pj in sorted((ARCH / "profiles").glob("*/profile.json")):
        d = json.loads(pj.read_text(encoding="utf-8"))
        src = pj.parent
        t = store.Trader(**{k: v for k, v in d.items() if k in store.Trader.__dataclass_fields__})
        t.save()
        t.analysis_dir.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        if (src / "activity.csv").exists():
            act = pd.read_csv(src / "activity.csv", low_memory=False)
            act = store.normalise_activity(act[[c for c in store.ACTIVITY_COLS if c in act.columns]])
            store.save_activity(t, act)
            print(f"{t.id}: activity {len(act):,} rows -> parquet ({time.time() - t0:.0f}s)")
        a = src / "analysis"
        for f in ("summary.json", "report.md", "windows.csv", "timing.csv", "price.csv", "hourly.csv", "daily.csv",
                  "edge_deciles.csv"):
            if (a / f).exists():
                shutil.copy2(a / f, t.analysis_dir / f)
        if (a / "buys.csv").exists():
            head = pd.read_csv(a / "buys.csv", nrows=0).columns
            b = pd.read_csv(a / "buys.csv", usecols=[c for c in BUY_COLS if c in head], low_memory=False)
            for c in ("slug", "o", "winner", "transactionHash"):
                if c in b:
                    b[c] = b[c].astype("string")
            b.to_parquet(t.analysis_dir / "buys.parquet", index=False, compression="zstd")
            print(f"{t.id}: buys {len(b):,} rows -> parquet")
        if (src / "last_job.log").exists():
            shutil.copy2(src / "last_job.log", t.log_path)
        rows = []
        for f in sorted((src / "live").glob("events-*.jsonl")):
            for ev in jsonl(f):
                rows.append((t.id, ev.get("recv_ms"), ev.get("ts"), ev.get("kind"), ev.get("source"), ev.get("side"),
                             ev.get("outcome"), ev.get("price"), ev.get("size"), ev.get("usdc"), ev.get("slug"),
                             ev.get("tx"), db.dumps(ev)))
        if rows:
            db.many("INSERT INTO trader_events (trader_id, recv_ms, ts, kind, source, side, outcome, price, size, usdc, "
                    "slug, tx, data) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            print(f"{t.id}: {len(rows):,} live events -> DB")

    # 3. the paper bot
    bd = ARCH / "bot_data"
    if bd.exists():
        from backend.bots import registry
        cfg = json.loads((bd / "config.json").read_text()) if (bd / "config.json").exists() else {}
        if not db.one("SELECT id FROM bots WHERE id = 'crouch-mm'"):
            registry.create_bot("CrouchPredictions MM", "market_maker", cfg, bot_id="crouch-mm")
        accounts = [p for p in sorted((bd / "archive").glob("*")) if p.is_dir()] + [bd]
        for n, folder in enumerate(accounts, start=1):
            fills = [f for p in sorted(folder.glob("fills-*.jsonl")) for f in jsonl(p)]
            db.many("INSERT INTO bot_fills (bot_id, account, ts, slug, win, side, price, shares, usd, maker, rebate, fee, "
                    "t_in, fair, btc, markout) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [("crouch-mm", n, r["ts"], r["slug"], r["win"], r["side"], r["price"], r["shares"], r["usd"],
                      int(bool(r.get("maker"))), r.get("rebate"), r.get("fee"), r.get("t_in"), r.get("fair"),
                      r.get("btc"), r.get("markout")) for r in fills])
            wins = {}
            for r in jsonl(folder / "windows.jsonl"):
                wins[r["slug"]] = r
            db.many("INSERT OR REPLACE INTO bot_windows (bot_id, account, slug, start, win, net_pnl, data) "
                    "VALUES (?,?,?,?,?,?,?)", [("crouch-mm", n, r["slug"], r["start"], r["win"], r.get("net_pnl"),
                                                db.dumps(r)) for r in wins.values()])
            eq = jsonl(folder / "equity.jsonl")
            db.many("INSERT INTO bot_equity (bot_id, account, ts, v, cash) VALUES (?,?,?,?,?)",
                    [("crouch-mm", n, e["ts"], e["v"], e.get("cash")) for e in eq])
            print(f"bot account {n}: {len(fills):,} fills, {len(wins):,} windows, {len(eq):,} equity points")
        acc = json.loads((bd / "account.json").read_text()) if (bd / "account.json").exists() else {}
        db.x("UPDATE bots SET account = ?, account_since = ?, bankroll = ? WHERE id = 'crouch-mm'",
             (len(accounts), acc.get("since") or time.time(), acc.get("bankroll") or cfg.get("bankroll", 1000.0)))
    print("migration done")


if __name__ == "__main__":
    main()
