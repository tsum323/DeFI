"""One live update of the published dashboard (run by .github/workflows/live.yml).

  python -m scripts.live_update --out _site

1. Make sure the database exists: if not, restore pool data and research state from
   data_snapshots/ (then the incremental sync fills the gap).
2. Load new blocks (free dRPC), evaluate the market (signal + virtual portfolio),
   update forward tests.
3. Write the static site to --out: the committed build in site/ (assets and the
   per-strategy charts, which depend only on the stored study) plus fresh data/*.json.
4. If the signal changed, write --notify-file with a message (the workflow turns it
   into a GitHub issue = push notification in the GitHub app).
Nothing here can trade: every RPC call goes through the read-only guard.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MSK = timezone(timedelta(hours=3), "МСК")
SITE_URL = "https://tsum323.github.io/DeFI/"
LIVE_STATE = "state_cache/research_db"  # restored from / saved to the Actions cache
TYPE_RU = {"OPEN CANDIDATE": "КАНДИДАТ НА ОТКРЫТИЕ", "WAIT": "ЖДАТЬ", "HOLD": "ДЕРЖАТЬ",
           "REBALANCE": "ПЕРЕСТАВИТЬ ДИАПАЗОН", "CLOSE CANDIDATE": "КАНДИДАТ НА ЗАКРЫТИЕ",
           "INVALIDATED": "СИГНАЛ ОТМЕНЁН"}

REASON_RU = {"data_current": "данные пула не свежие", "pool_activity": "в пуле мало торгов",
             "market_data_complete": "не хватает рыночных данных",
             "no_validated_strategy": "ни одна стратегия не прошла все проверки"}


def run(*args: str, check: bool = True) -> int:
    print("$ python -m", " ".join(args), flush=True)
    r = subprocess.run([sys.executable, "-m", *args], cwd=ROOT)
    if check and r.returncode != 0:
        raise SystemExit(f"step failed: {' '.join(args)} (exit {r.returncode})")
    return r.returncode


def latest_signal(engine):
    from sqlalchemy import select

    from backend.database import schema as S
    with engine.connect() as c:
        s = c.execute(select(S.signals).order_by(S.signals.c.id.desc()).limit(1)).first()
        if s is None:
            return None
        ev = c.execute(select(S.signal_events).where(S.signal_events.c.signal_id == s.id)
                       .order_by(S.signal_events.c.id.desc()).limit(1)).first()
    return {"id": s.id, "type": s.signal_type, "status": ev.status if ev else s.signal_type,
            "reason": ev.reason if ev else "", "payload": json.loads(s.payload_json)}


def watch_names(engine) -> dict[int, str]:
    from sqlalchemy import select

    from backend.database import schema as S
    from backend.strategies.generator import params_from_json
    from backend.virtual_portfolio.portfolio import WATCH_PREFIX
    with engine.connect() as c:
        return {r.id: f"{params_from_json(r.params_json).name}: {r.reason.removeprefix(WATCH_PREFIX)}"
                for r in c.execute(select(S.forward_tests).where(
                    S.forward_tests.c.reason.startswith(WATCH_PREFIX)))}


def message(sig: dict) -> tuple[str, str]:
    p, st = sig["payload"], sig["status"]
    title = f"Сигнал WBTC/USDC: {TYPE_RU.get(st, st)}"
    lines = [f"**{TYPE_RU.get(st, st)}** (сигнал #{sig['id']}, {TYPE_RU.get(sig['type'], sig['type'])})", ""]
    if sig["type"] == "OPEN CANDIDATE" and st == "OPEN CANDIDATE":
        r, a = p["range"], p["allocation"]
        lines += [f"- Стратегия: `{p['strategy']}`, уверенность: {p['confidence']}",
                  f"- Диапазон: {r['price_lower']:,.0f} – {r['price_upper']:,.0f} USDC (цена {p['current_price']:,.0f})",
                  f"- Внести: {a['token0']:.6f} WBTC + {a['token1']:,.2f} USDC",
                  f"- Комиссии за {p['horizon_days']} дн.: ${p['expected_fees_usd'][0]:,.2f} – "
                  f"${p['expected_fees_usd'][1]:,.2f}; расходы ${p['expected_costs_usd']['total']:,.2f}",
                  f"- Действует до {datetime.fromtimestamp(p['valid_until'], MSK):%d.%m.%Y %H:%M} МСК"]
    else:
        try:
            why = "; ".join(REASON_RU.get(r, r) for r in json.loads(sig["reason"]))
        except (ValueError, TypeError):
            why = sig["reason"]
        lines.append(f"Причина: {why}")
    lines += ["", f"Подробности: {SITE_URL} (раздел «Центр сигналов»).", "",
              "_Исследовательский сигнал, не инвестиционная рекомендация. Система ничего не исполняет._"]
    return title, "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="_site")
    ap.add_argument("--notify-file", default="notify.json")
    args = ap.parse_args()
    t0 = time.monotonic()
    from backend.core.config import load_config
    from backend.database import schema as S
    cfg = load_config()
    db = ROOT / cfg.lab["database"]["url"].removeprefix("sqlite:///")
    if not db.exists() or db.stat().st_size < 10 ** 8:
        print("no database in cache: restoring from data_snapshots/", flush=True)
        run("scripts.data_snapshot", "restore")
    # Side files (pool verification, ETH/USD history, ...) are not in the Actions cache
    # with the database: always take missing ones from the committed snapshot.
    from scripts.data_snapshot import SIDE_FILES
    snap = ROOT / "data_snapshots" / cfg.primary_pool
    for name in SIDE_FILES:
        if (snap / name).exists() and not (ROOT / "data" / name).exists():
            shutil.copy2(snap / name, ROOT / "data" / name)
    # idempotent merges: first the committed state (new experiments pushed from a session),
    # then the live state kept in the Actions cache (signals, forward tests since the last commit)
    run("scripts.data_snapshot", "restore-research")
    if (ROOT / LIVE_STATE / "manifest.json").exists():
        run("scripts.data_snapshot", "restore-research", "--research-dir", LIVE_STATE)
    engine = S.make_engine(cfg.lab["database"]["url"])
    before = latest_signal(engine)
    watch_before = watch_names(engine)
    run("scripts.sync_pool", "--endpoint", "drpc", "--max-minutes", "15", check=False)
    run("scripts.evaluate_market")
    run("scripts.forward_test", check=False)
    after = latest_signal(engine)
    new_watch = [v for k, v in watch_names(engine).items() if k not in watch_before]

    out = ROOT / args.out
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(ROOT / "site", out)
    from fastapi.testclient import TestClient

    from backend.api.app import app
    c = TestClient(app)
    data = out / "data"
    have = {p.stem.removeprefix("strategy_") for p in data.glob("strategy_*.json")}
    for name in ("overview", "strategies", "research", "signals", "portfolio", "datasources"):
        doc = c.get(f"/api/{name}").json()
        if name == "strategies":
            for r in doc["rows"]:
                r["has_details"] = r["spec_hash"] in have
        (data / f"{name}.json").write_text(json.dumps(doc))
    ds = json.loads((data / "datasources.json").read_text())
    (data / "snapshot.json").write_text(json.dumps({
        "exported_at": int(time.time()), "last_block": ds["last_block"],
        "last_block_ts": ds["last_block_ts"], "live": True, "every_minutes": 15}))

    notify = None
    # notify when the status changes (a new WAIT with other reasons is not news)
    if after and (before is None or after["status"] != before["status"]
                  or (after["id"] != before["id"] and after["type"] != "WAIT")):
        title, body = message(after)
        notify = {"title": title, "body": body}
    elif new_watch:
        notify = {"title": "WBTC/USDC: новая стратегия в наблюдении",
                  "body": "\n".join([("Стратегии, которым до сигнала не хватает одной проверки, взяты на "
                                      "виртуальный тест на новых данных. **Это не сигнал на открытие.**"), ""]
                                    + [f"- {x}" for x in new_watch]
                                    + ["", f"Подробности: {SITE_URL} (раздел «Центр сигналов» → «Наблюдение»)."])}
    Path(args.notify_file).write_text(json.dumps(notify) if notify else "")
    print(f"site written to {out} in {time.monotonic() - t0:.0f}s; "
          f"signal {after['type'] if after else None}/{after['status'] if after else None}"
          + (f"; NOTIFY: {notify['title']}" if notify else ""))


if __name__ == "__main__":
    main()
