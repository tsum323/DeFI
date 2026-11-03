"""«Мои позиции»: a chosen signal (owner's GitHub issue) is followed live (SYNTHETIC)."""

from __future__ import annotations

import json

from backend.core.config import load_config
from backend.database import schema as S
from backend.lab import tracking
from tests.test_bot import BASE, hours, pool_entry


class FakeGH:
    ok = True

    def __init__(self, issues):
        self._issues, self.comments = issues, []

    def issues(self, state="all"):
        return self._issues

    def comment(self, number, text):
        self.comments.append((number, text))


def issue(n, body, assoc="OWNER", state="open"):
    return {"number": n, "body": body, "author_association": assoc, "state": state}


def block(**kw):
    d = {"pool": "base_weth_usdc_500", "price_lower": 2700, "price_upper": 3300, "amount_usd": 200,
         "net_usd": [1.0, 3.0], "horizon_days": 7}
    d.update(kw)
    return "Выбранный сигнал\n```track\n" + json.dumps(d) + "\n```"


def test_only_owner_issues_with_a_valid_block_are_accepted():
    cfg = load_config()
    assert tracking.parse_issue(issue(1, block()), cfg)["amount_usd"] == 200
    assert tracking.parse_issue(issue(2, block(), assoc="NONE"), cfg) is None          # public repo: strangers ignored
    assert tracking.parse_issue(issue(3, block(pool="nope")), cfg) is None
    assert tracking.parse_issue(issue(4, block(price_lower=3400)), cfg) is None
    assert tracking.parse_issue(issue(5, "no block"), cfg) is None


def test_follow_advise_comment_and_close(tmp_path):
    engine = S.make_engine(f"sqlite:///{tmp_path / 'lab.sqlite'}")
    cfg = load_config()
    b_pool, sp, pos = pool_entry(BASE, gas=0.02)
    b_pool.update(gas_open_close_usd=0.02, network_ru="Base")
    scan = {"capital_usd": 10_000.0, "horizon_days": 7, "eth_usd": 3000.0, "btc_usd": 80_000.0, "pools": [b_pool]}
    gh = FakeGH([issue(7, block())])
    t0 = 3600 * 1000
    r = tracking.sync(engine, cfg, scan, {BASE.key: BASE}, {}, gh, now=t0)
    assert r["new"] == 1 and gh.comments[0][0] == 7 and "Принято" in gh.comments[0][1]
    inside = hours(BASE, sp, pos, 12, t0)
    tracking.sync(engine, cfg, scan, {BASE.key: BASE}, {BASE.key: inside}, gh, now=t0 + 12 * 3600)
    s = tracking.summary(engine)
    assert s["open"][0]["advice"]["code"] == "hold" and s["open"][0]["mark"]["fees_usd"] > 0
    out = inside + hours(BASE, sp, pos, 30, t0 + 12 * 3600, inside=False)
    tracking.sync(engine, cfg, scan, {BASE.key: BASE}, {BASE.key: out}, gh, now=t0 + 42 * 3600)
    s = tracking.summary(engine)
    assert s["open"][0]["advice"]["code"] == "out_long" and "Пора решать" in gh.comments[-1][1]
    assert [h["code"] for h in s["open"][0]["history"]] == ["hold", "out_long"]
    gh._issues = [issue(7, block(), state="closed")]
    assert tracking.sync(engine, cfg, scan, {BASE.key: BASE}, {BASE.key: out}, gh, now=t0 + 43 * 3600)["closed"] == 1
    assert tracking.summary(engine)["closed"][0]["mark"]["net_usd"] is not None
