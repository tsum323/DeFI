"""Strategy parameters in plain Russian (for the user interface)."""

from __future__ import annotations

import re

from backend.strategies.generator import StrategyParams

DAY = 86_400


def _delay(blocks: int) -> str:
    if blocks <= 0:
        return "сразу"
    hours = blocks * 12 / 3600
    return f"через ~{hours:.0f} ч" if hours < 20 else f"через ~{hours / 24:.0f} сут."


def describe(p: StrategyParams, base: str = "WBTC", quote: str = "USDC") -> str:
    """e.g. w0.3_r1=0.7_exit_d300 -> 'диапазон ±30% от цены; 70% в USDC, 30% в WBTC;
    если цена выйдет из диапазона — новый диапазон вокруг цены через ~1 ч'."""
    c = p.canonical()
    if c.full_range:
        return "весь диапазон цен (без концентрации)"
    if c.width_vol_mult is not None:
        parts = [f"ширина диапазона подстраивается под волатильность ({c.width_vol_mult:g} × недельная)"]
    else:
        parts = [f"диапазон ±{c.width_pct:.0%} от цены"]
    parts.append(f"{c.ratio_token1:.0%} в {quote}, {1 - c.ratio_token1:.0%} в {base}" if c.ratio_token1 is not None
                 else f"{base} и {quote} примерно поровну")
    if c.rebalance == "none":
        parts.append("диапазон не меняется")
    elif c.rebalance == "on_exit":
        parts.append(f"если цена выйдет из диапазона — новый диапазон вокруг цены {_delay(c.rebalance_delay_blocks)}")
    elif c.rebalance == "periodic":
        parts.append(f"диапазон заново ставится вокруг цены каждые {c.period_seconds // DAY} дн.")
    else:
        parts.append(f"диапазон переставляется, когда цена прошла {c.edge_fraction:.0%} пути до края")
    if c.cooldown_seconds:
        h = c.cooldown_seconds // 3600
        parts.append(f"не чаще раза в {h // 24} сут." if h % 24 == 0 else f"не чаще раза в {h} ч")
    if c.max_vol_entry is not None:
        parts.append(f"выходит из позиции, если рынок слишком нервный (волатильность выше {c.max_vol_entry:.0%})")
    return "; ".join(parts)


def describe_static(label: str) -> str:
    """'static ±10%' (scanner positions) -> plain text."""
    m = re.search(r"±(\d+)%", label)
    return f"диапазон ±{m.group(1)}% от цены; диапазон не меняется" if m else label
