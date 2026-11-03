"""«Мои позиции»: signals the user chose, followed live.

The site has no server, so a choice reaches the system as a GitHub issue that the
"Выбрать" button pre-fills (label `track`, a ```track {json}``` block: pool, price range,
amount). Every update reads open `track` issues WRITTEN BY THE REPOSITORY OWNER (the repo is
public: nobody else can add positions), values each position as the static range the user
opened in Uniswap (hourly pool data, ESTIMATED), and gives advice: hold / out of range /
re-centre / move / review. When the advice changes, a comment is posted to the issue (a push
notification in the GitHub app). Closing the issue freezes the final result.
Read-only towards the blockchain; the only write is a comment on the user's own issue.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.request

from sqlalchemy import insert, select, update

from backend.database import schema as S

LABEL = "track"
BLOCK = re.compile(r"```track\s*(\{.*?\})\s*```", re.DOTALL)


# ---------------------------------------------------------------- GitHub (issues of this repo)
class GitHub:
    def __init__(self, repo: str | None = None, token: str | None = None):
        self.repo = repo or os.environ.get("GITHUB_REPOSITORY", "")
        self.token = token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")

    @property
    def ok(self) -> bool:
        return bool(self.repo and self.token)

    def _req(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(f"https://api.github.com/repos/{self.repo}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                                              "User-Agent": "uniswap-ai-lab"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"null")

    def issues(self, state: str = "all") -> list[dict]:
        """Issues opened by the repository owner that carry a ```track``` block (the label
        is optional: a pre-filled link cannot always set it)."""
        owner = self.repo.split("/")[0]
        items = self._req("GET", f"/issues?creator={owner}&state={state}&per_page=100&sort=created")
        return [i for i in items if "pull_request" not in i and BLOCK.search(i.get("body") or "")]

    def comment(self, number: int, text: str) -> None:
        self._req("POST", f"/issues/{number}/comments", {"body": text})


def parse_issue(issue: dict, cfg) -> dict | None:
    """The ```track {...}``` block of an owner's issue, validated, or None."""
    if issue.get("author_association") != "OWNER":
        return None
    m = BLOCK.search(issue.get("body") or "")
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
        pool = str(d["pool"])
        lo, hi, amount = float(d["price_lower"]), float(d["price_upper"]), float(d["amount_usd"])
    except (ValueError, KeyError, TypeError):
        return None
    if pool not in cfg.pools or not (0 < lo < hi) or not (1 <= amount <= 10_000_000):
        return None
    return {"pool": pool, "price_lower": lo, "price_upper": hi, "amount_usd": amount,
            "card": {k: d.get(k) for k in ("card_id", "strategy", "tier", "fees_usd", "net_usd", "horizon_days")}}


# ---------------------------------------------------------------- valuation of a static range
def _sqrt_raw(price_base: float, p) -> float:
    """sqrt of token1/token0 in raw units for a BASE price (quote per base)."""
    x = price_base if p.base == p.token0 else 1 / price_base
    return math.sqrt(x * 10 ** (p.dec1 - p.dec0))


def range_position(p, lo: float, hi: float, entry_price: float, amount_usd: float, usd: dict) -> dict:
    from backend.lab.scanner import amounts_for_liquidity
    sa, sb = sorted((_sqrt_raw(lo, p), _sqrt_raw(hi, p)))
    sp = _sqrt_raw(entry_price, p)
    u0, u1 = usd[p.token0], usd[p.token1]
    a0, a1 = amounts_for_liquidity(1.0, sp, sa, sb)
    per_l = a0 / 10 ** p.dec0 * u0 + a1 / 10 ** p.dec1 * u1
    L = amount_usd / per_l
    return {"L": L, "sa": sa, "sb": sb, "amount0": a0 * L / 10 ** p.dec0, "amount1": a1 * L / 10 ** p.dec1}


def value(p, e: dict, hours: list, usd_now: dict) -> dict:
    """Fees since the start (hours whose close was in range x our share of the active
    liquidity), the position at the latest close, and the same tokens simply held."""
    from backend.lab.scanner import ASSET, amounts_for_liquidity, base_price
    usd = {t: {"USD": 1.0, "ETH": usd_now["ETH"], "BTC": usd_now["BTC"]}[a] for t, a in ASSET.items()}
    u0, u1 = usd[p.token0], usd[p.token1]
    later = [h for h in hours if h.hour_ts >= e["since_hour"]]
    fees = inside = 0.0
    for h in later:
        sp = float(h.close_sqrt_price) / 2 ** 96
        if e["sa"] <= sp < e["sb"]:
            fees += (h.fee0 * u0 + h.fee1 * u1) * e["L"] / (float(h.close_liquidity) + e["L"])
            inside += 1
    if not later:
        return {"days": 0.0, "value_usd": e["amount_usd"] - e["costs_usd"], "fees_usd": 0.0, "il_usd": 0.0,
                "net_usd": -e["costs_usd"], "vs_hodl_usd": -e["costs_usd"], "in_range": True, "time_in_range": 1.0,
                "price": e["price"], "hours_out": 0, "label": "ESTIMATED"}
    last = later[-1]
    sp = float(last.close_sqrt_price) / 2 ** 96
    a0, a1 = amounts_for_liquidity(e["L"], sp, e["sa"], e["sb"])
    lp = a0 / 10 ** p.dec0 * u0 + a1 / 10 ** p.dec1 * u1
    hold = e["amount0"] * u0 + e["amount1"] * u1
    out_h = 0
    for h in reversed(later):   # consecutive hours out of range up to now
        s_ = float(h.close_sqrt_price) / 2 ** 96
        if e["sa"] <= s_ < e["sb"]:
            break
        out_h += 1
    net = lp + fees - e["costs_usd"] - e["amount_usd"]
    return {"days": (last.hour_ts - e["since_hour"]) / 86_400 + 1 / 24, "value_usd": e["amount_usd"] + net,
            "fees_usd": fees, "il_usd": lp - hold, "net_usd": net, "vs_hodl_usd": lp + fees - e["costs_usd"] - hold,
            "in_range": e["sa"] <= sp < e["sb"], "time_in_range": inside / len(later),
            "price": base_price(int(last.close_sqrt_price), p), "hours_out": out_h, "label": "ESTIMATED"}


def advise(m: dict, e: dict, best: dict | None) -> dict:
    """Plain advice for the user's own position."""
    if m["hours_out"] >= 24:
        side = "выше" if m["price"] > e["range"][1] else "ниже"
        alt = (f" Лучший вариант сейчас: {best['pair']} {best['fee_tier']:.2%} в сети {best['network_ru']}, "
               f"диапазон {best['price_lower']:,.4g}–{best['price_upper']:,.4g}." if best else "")
        return {"code": "out_long", "title": "Пора решать: цена давно вне диапазона",
                "text": f"Цена {side} вашего диапазона уже {m['hours_out']} ч — комиссии не идут, позиция целиком в одной "
                        f"монете. Варианты: закрыть позицию или переставить диапазон вокруг текущей цены.{alt}"}
    if not m["in_range"]:
        return {"code": "out", "title": "Цена вышла из диапазона",
                "text": f"Цена {m['price']:,.4g} — за пределами {e['range'][0]:,.4g}–{e['range'][1]:,.4g}. Комиссии пока не "
                        "начисляются. Часто цена возвращается; если будет вне диапазона больше суток — система предложит действие."}
    if best and best.get("pool") != e["pool"] and best.get("net_mid_usd", 0) > 3 * max(e.get("forecast_mid", 0), 0) + 1:
        return {"code": "move", "title": "Есть заметно лучший вариант",
                "text": f"{best['pair']} {best['fee_tier']:.2%} в сети {best['network_ru']} сейчас ожидаемо приносит намного "
                        f"больше на вашу сумму (~${best['net_mid_usd']:,.2f} за неделю против ~${e.get('forecast_mid', 0):,.2f}). "
                        "Переезд стоит газа и обмена — решайте, учитывая это."}
    if m["days"] >= e.get("horizon_days", 7):
        return {"code": "review", "title": "Неделя прошла — время подвести итог",
                "text": f"Итог за {m['days']:.1f} дн.: ${m['net_usd']:+,.2f} (к простому хранению ${m['vs_hodl_usd']:+,.2f}). "
                        "Можно держать дальше, пока цена в диапазоне, или забрать комиссии."}
    return {"code": "hold", "title": "Держать",
            "text": f"Цена {m['price']:,.4g} внутри диапазона, комиссии начисляются (уже ${m['fees_usd']:,.2f})."}


# ---------------------------------------------------------------- the step of the routine
def sync(engine, cfg, scan: dict, scan_pools: dict, hours_by_pool: dict, gh: GitHub | None = None,
         now: int | None = None, log=print) -> dict:
    from backend.lab.activity import log_activity
    from backend.lab.bot import options
    from backend.lab.scanner import ASSET
    now = now or int(time.time())
    gh = gh or GitHub()
    out = {"new": 0, "closed": 0, "updated": 0, "comments": 0}
    if not gh.ok:
        return {**out, "skipped": "no GitHub token"}
    issues = gh.issues("all")
    by_num = {i["number"]: i for i in issues}
    usd_now = {"ETH": scan["eth_usd"], "BTC": scan["btc_usd"]}
    with engine.connect() as c:
        known = {r.issue_number: r for r in c.execute(select(S.tracked_positions))}
    by_pool_scan = {p["pool"]: p for p in scan.get("pools", [])}
    # new choices
    for i in issues:
        if i["number"] in known or i["state"] != "open":
            continue
        d = parse_issue(i, cfg)
        if d is None or d["pool"] not in scan_pools or d["pool"] not in by_pool_scan:
            continue
        p, sp = scan_pools[d["pool"]], by_pool_scan[d["pool"]]
        usd = {t: {"USD": 1.0, "ETH": usd_now["ETH"], "BTC": usd_now["BTC"]}[a] for t, a in ASSET.items()}
        pos = range_position(p, d["price_lower"], d["price_upper"], sp["price"], d["amount_usd"], usd)
        base_share = (pos["amount0"] * usd[p.token0] if p.base == p.token0 else pos["amount1"] * usd[p.token1]) / d["amount_usd"]
        costs = sp["gas_open_close_usd"] + d["amount_usd"] * base_share * p.fee / 1e6
        card = d["card"]
        e = {**pos, "pool": d["pool"], "price": sp["price"], "range": [d["price_lower"], d["price_upper"]],
             "amount_usd": d["amount_usd"], "costs_usd": costs, "since_hour": now - now % 3600,
             "horizon_days": card.get("horizon_days") or 7,
             "forecast_mid": (sum(card["net_usd"]) / 2) if card.get("net_usd") else 0.0, "card": card}
        with engine.begin() as c:
            c.execute(insert(S.tracked_positions).values(
                issue_number=i["number"], created_at=now, pool=d["pool"], network=p.network, pair=sp["pair"],
                amount_usd=d["amount_usd"], price_lower=d["price_lower"], price_upper=d["price_upper"],
                entry_json=json.dumps(e), status="open", history_json="[]"))
        gh.comment(i["number"], f"✅ Принято. Отслеживаю с цены {sp['price']:,.4g}: {sp['pair']} {sp['fee_tier']:.2%} "
                                f"({sp.get('network_ru', '')}), диапазон {d['price_lower']:,.4g}–{d['price_upper']:,.4g}, "
                                f"${d['amount_usd']:,.2f}. Совет будет обновляться каждые 15 минут на сайте "
                                f"(раздел «Мои позиции»); при смене совета напишу сюда.\n\n_Оценка модели (ESTIMATED), не гарантия._")
        log_activity(engine, "tracking", "info", f"Вы выбрали сигнал: {sp['pair']} ({sp.get('network_ru', '')}) на "
                     f"${d['amount_usd']:,.2f} — начато отслеживание")
        out["new"] += 1
        out["comments"] += 1
    # update open ones
    with engine.connect() as c:
        rows = c.execute(select(S.tracked_positions).where(S.tracked_positions.c.status == "open")).all()
    for r in rows:
        e = json.loads(r.entry_json)
        p = scan_pools.get(r.pool)
        if p is None:
            continue
        m = value(p, e, hours_by_pool.get(r.pool, []), usd_now)
        opts = options(scan, max(m["value_usd"], 1.0), {}, 0.5)
        best = opts[0] if opts else None
        a = advise(m, e, best)
        hist = json.loads(r.history_json or "[]")
        issue = by_num.get(r.issue_number)
        vals = {"mark_json": json.dumps(m), "advice_json": json.dumps(a)}
        if not hist or hist[-1]["code"] != a["code"]:
            hist.append({"at": now, **a, "net_usd": m["net_usd"]})
            vals["history_json"] = json.dumps(hist)
            if issue and issue["state"] == "open" and len(hist) > 1:
                gh.comment(r.issue_number, f"**{a['title']}**\n\n{a['text']}\n\nСейчас: стоимость ${m['value_usd']:,.2f}, "
                                           f"комиссии ${m['fees_usd']:,.2f}, итог ${m['net_usd']:+,.2f}.")
                out["comments"] += 1
        if issue and issue["state"] == "closed":
            vals.update(status="closed", closed_at=now)
            log_activity(engine, "tracking", "info", f"Позиция {r.pair} закрыта вами: итог ${m['net_usd']:+,.2f} "
                         f"(к хранению ${m['vs_hodl_usd']:+,.2f})")
            out["closed"] += 1
        with engine.begin() as c:
            c.execute(update(S.tracked_positions).where(S.tracked_positions.c.id == r.id).values(**vals))
        out["updated"] += 1
    return out


def summary(engine) -> dict:
    with engine.connect() as c:
        rows = c.execute(select(S.tracked_positions).order_by(S.tracked_positions.c.id.desc())).all()
    items = [{"id": r.id, "issue": r.issue_number, "created_at": r.created_at, "pool": r.pool, "network": r.network,
              "pair": r.pair, "amount_usd": r.amount_usd, "range": [r.price_lower, r.price_upper],
              "entry": {k: v for k, v in json.loads(r.entry_json).items() if k not in ("L", "sa", "sb")},
              "mark": json.loads(r.mark_json) if r.mark_json else None,
              "advice": json.loads(r.advice_json) if r.advice_json else None,
              "history": json.loads(r.history_json or "[]"), "status": r.status, "closed_at": r.closed_at}
             for r in rows]
    return {"open": [i for i in items if i["status"] == "open"], "closed": [i for i in items if i["status"] == "closed"],
            "repo": os.environ.get("GITHUB_REPOSITORY", "tsum323/DeFI"), "label": "ESTIMATED"}
