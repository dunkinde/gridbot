#!/usr/bin/env python3
"""
Find Binance spot pairs where a grid is mechanically viable at a small budget.

    python find_pair.py --budget 50
    python find_pair.py --budget 50 --fee 0.00075      # if you pay fees in BNB
    python find_pair.py --budget 50 --quote EUR
    python find_pair.py --budget 50 --testnet          # screen the sandbox's pairs

WHAT THIS CHECKS

A grid at a small budget dies for three mechanical reasons, long before the
strategy itself is even in question:

  1. MIN NOTIONAL. Every order must clear the exchange's minimum (5 USDT on
     most Binance spot pairs). budget/grids has to stay comfortably above it,
     which caps how many grids a small budget can have.

  2. LOT-SIZE QUANTIZATION. Order amounts must be whole multiples of the pair's
     `stepSize`. Each fill therefore discards up to one step of base, worth
     (stepSize * price) in quote terms. That has to be small next to the profit
     of one round trip, or rounding simply eats the edge. This is what rules out
     BTC at any small budget: one 0.00001 BTC step is about $1, against an edge
     measured in cents.

  3. FEES. A round trip earns one grid step and pays two lots of fees. Fewer,
     wider cells raise the edge per trip; more, tighter cells lower it. At a
     small budget you are pushed toward few wide cells, which then need a
     volatile pair to fill at all.

It then ranks survivors by a CRUDE fill estimate from recent realised range.
That ranking is a feasibility hint, not a profit forecast — it assumes price
oscillates through the grid, which is exactly the assumption that fails in a
trend. Read the caveats printed at the end.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Optional

try:
    import ccxt
except ImportError:
    raise SystemExit("pip install ccxt")


# --------------------------------------------------------------------------
# Pure logic — no network, unit-testable
# --------------------------------------------------------------------------
@dataclass
class PairInfo:
    symbol: str
    price: float
    amount_step: float
    min_cost: float
    min_amount: float
    daily_range_pct: float       # (high-low)/close over the lookback
    quote_volume: float          # 24h, in quote currency


@dataclass
class Candidate:
    symbol: str
    grids: int
    range_pct: float
    per_cell: float
    step_pct: float
    net_edge_pct: float
    edge_quote: float            # profit per completed round trip
    quantum: float               # quote value of one lot-size step
    quantum_ratio: float         # quantum / edge_quote  (want << 1)
    fee_drag: float              # 2*fee / step -- fraction of gross eaten by fees
    trips_per_day: float
    est_daily: float
    price: float
    daily_range_pct: float

    def __str__(self) -> str:
        return (f"{self.symbol:<14} {self.grids:>3}  ±{self.range_pct:>5.1%} "
                f"{self.per_cell:>7.2f} {self.step_pct:>7.2%} "
                f"{self.edge_quote:>8.4f} {self.quantum:>8.4f} "
                f"{self.quantum_ratio:>6.2f} {self.fee_drag:>6.0%} "
                f"{self.trips_per_day:>7.2f} {self.est_daily:>8.3f}")


def geometric_step(range_pct: float, grids: int) -> float:
    """Percentage step of a geometric grid spanning +/- range_pct."""
    lower, upper = 1.0 - range_pct, 1.0 + range_pct
    return (upper / lower) ** (1.0 / grids) - 1.0


def evaluate(p: PairInfo, budget: float, grids: int, range_pct: float,
             fee: float, min_notional_safety: float = 2.0,
             max_quantum_ratio: float = 0.25,
             max_fee_drag: float = 0.5) -> Optional[Candidate]:
    """Return a Candidate if this (pair, grids, range) is mechanically viable."""
    per_cell = budget / grids
    step = geometric_step(range_pct, grids)
    net_edge = step - 2 * fee
    if net_edge <= 0:
        return None

    # --- shape constraints: is this still a GRID, and can it actually fill? ---
    # Without these the search degenerates to 2 enormous cells, because the
    # profit model (trips x edge-per-trip) is scale-invariant while fees are
    # amortised over fewer trips. A 2-cell grid at +/-20% is not a grid; it is
    # one limit order that fills roughly never.
    if grids < 4:
        return None
    # A cell must be crossable within a typical day, or fills are too rare to
    # model honestly.
    if step > p.daily_range_pct:
        return None
    # The band should cover roughly a day's movement, or price sits outside it.
    if range_pct < p.daily_range_pct * 0.75:
        return None
    # Fees must not eat most of the gross step.
    if (2 * fee) / step > max_fee_drag:
        return None

    # 1. min notional, with headroom -- an order that dips below it is rejected
    if per_cell < p.min_cost * min_notional_safety:
        return None
    # 2. min order quantity at the top of the range (the smallest qty we'd send)
    top = p.price * (1 + range_pct)
    if p.min_amount and (per_cell / top) < p.min_amount:
        return None
    # 3. lot-size quantization vs the edge
    quantum = p.amount_step * top
    edge_quote = per_cell * net_edge
    if quantum > max_quantum_ratio * edge_quote:
        return None
    if per_cell / top < p.amount_step:        # qty would round to zero
        return None

    # Crude fill estimate: how many step-widths does a typical day traverse?
    # Halved because a round trip needs a down-move AND an up-move.
    # Only the cells a typical day actually reaches can trade.
    reach = min(p.daily_range_pct / (2 * range_pct), 1.0)
    active_cells = grids * reach
    crossings = p.daily_range_pct / step
    trips_per_day = min(crossings / 2, active_cells)
    return Candidate(
        symbol=p.symbol, grids=grids, range_pct=range_pct, per_cell=per_cell,
        step_pct=step, net_edge_pct=net_edge, edge_quote=edge_quote,
        quantum=quantum, quantum_ratio=quantum / edge_quote,
        fee_drag=(2 * fee) / step,
        trips_per_day=trips_per_day, est_daily=trips_per_day * edge_quote,
        price=p.price, daily_range_pct=p.daily_range_pct)


def best_for_pair(p: PairInfo, budget: float, fee: float,
                  grid_choices=range(4, 21)) -> Optional[Candidate]:
    """Search grid counts and ranges; return the best viable config, if any."""
    best = None
    for grids in grid_choices:
        # Size the band off the pair's own realised range rather than a fixed %.
        for mult in (0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0):
            range_pct = max(p.daily_range_pct * mult, 0.005)
            if range_pct > 0.35:
                continue
            c = evaluate(p, budget, grids, range_pct, fee)
            if c and (best is None or c.est_daily > best.est_daily):
                best = c
    return best


# --------------------------------------------------------------------------
# Network
# --------------------------------------------------------------------------
def fetch_pairs(quote: str, testnet: bool, lookback_days: int,
                min_volume: float, limit_symbols: int) -> list:
    ex = ccxt.binance({"enableRateLimit": True,
                       "options": {"defaultType": "spot",
                                   "adjustForTimeDifference": True}})
    if testnet:
        ex.set_sandbox_mode(True)
    markets = ex.load_markets()
    tickers = ex.fetch_tickers()

    rows = []
    for sym, m in markets.items():
        if not m.get("spot") or not m.get("active"):
            continue
        if m.get("quote") != quote:
            continue
        t = tickers.get(sym)
        if not t or not t.get("last"):
            continue
        qv = float(t.get("quoteVolume") or 0.0)
        if qv < min_volume:
            continue

        prec = m.get("precision") or {}
        limits = m.get("limits") or {}

        def as_step(v, default):
            if v is None:
                return default
            v = float(v)
            return v if v < 1 else 10.0 ** (-int(v))

        price = float(t["last"])
        hi, lo = t.get("high"), t.get("low")
        if hi and lo and price:
            rng = (float(hi) - float(lo)) / price
        else:
            rng = 0.0
        rows.append(PairInfo(
            symbol=sym, price=price,
            amount_step=as_step(prec.get("amount"), 1e-8),
            min_cost=float((limits.get("cost") or {}).get("min") or 0.0),
            min_amount=float((limits.get("amount") or {}).get("min") or 0.0),
            daily_range_pct=rng, quote_volume=qv))

    rows.sort(key=lambda r: r.quote_volume, reverse=True)
    rows = rows[:limit_symbols]

    if lookback_days > 1:
        # 24h range is noisy; average the true daily range over a few days.
        print(f"refining volatility over {lookback_days}d for {len(rows)} pairs "
              f"(rate-limited, be patient)...", file=sys.stderr)
        for i, r in enumerate(rows):
            try:
                ohlcv = ex.fetch_ohlcv(r.symbol, "1d", limit=lookback_days)
                if ohlcv:
                    rngs = [(c[2] - c[3]) / c[4] for c in ohlcv if c[4]]
                    if rngs:
                        r.daily_range_pct = sum(rngs) / len(rngs)
            except Exception:
                pass
            if (i + 1) % 25 == 0:
                print(f"  {i + 1}/{len(rows)}", file=sys.stderr)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--budget", type=float, required=True)
    ap.add_argument("--quote", default="USDT")
    ap.add_argument("--fee", type=float, default=0.001,
                    help="per side; 0.00075 with the BNB discount")
    ap.add_argument("--testnet", action="store_true")
    ap.add_argument("--lookback-days", type=int, default=7)
    ap.add_argument("--min-volume", type=float, default=5_000_000,
                    help="minimum 24h quote volume; thin books make grids lie")
    ap.add_argument("--limit-symbols", type=int, default=120)
    ap.add_argument("--top", type=int, default=20)
    a = ap.parse_args()

    pairs = fetch_pairs(a.quote, a.testnet, a.lookback_days,
                        a.min_volume, a.limit_symbols)
    print(f"\nscreened {len(pairs)} liquid {a.quote} spot pairs "
          f"at budget {a.budget:,.0f} {a.quote}, fee {a.fee:.4%}/side\n")

    results = [c for c in (best_for_pair(p, a.budget, a.fee) for p in pairs) if c]
    results.sort(key=lambda c: c.est_daily, reverse=True)

    if not results:
        print("NOTHING PASSES.")
        print(f"\nAt {a.budget:,.0f} {a.quote} no liquid pair clears both the "
              f"minimum-notional floor and the lot-size test at once.")
        print("Options, least bad first:")
        print("  * keep running on the testnet, which costs nothing")
        print("  * save until the budget clears the floor for a pair you like")
        print("  * pay fees in BNB (--fee 0.00075) and re-run; it widens the net edge")
        return

    hdr = (f"{'pair':<14} {'grids':>3}  {'range':>6} {'per_cell':>7} {'step':>7} "
           f"{'/trip':>8} {'quantum':>8} {'q/e':>6} {'fees':>6} {'trips/d':>7} "
           f"{'est /d':>8}")
    print(hdr)
    print("-" * len(hdr))
    for c in results[:a.top]:
        print(c)

    best = results[0]
    print(f"\nBest mechanical fit: {best.symbol} with {best.grids} grids at "
          f"±{best.range_pct:.1%}")
    print(f"  per cell      {best.per_cell:,.2f} {a.quote}")
    print(f"  net edge      {best.net_edge_pct:.3%} = {best.edge_quote:,.4f} per round trip")
    print(f"  lot quantum   {best.quantum:,.4f} ({best.quantum_ratio:.0%} of one trip's profit)")
    print(f"  fee drag      {best.fee_drag:.0%} of the gross step goes to fees")
    print(f"  recent range  {best.daily_range_pct:.2%}/day")
    print(f"\n  python trading_bot.py --mode testnet --symbol {best.symbol} "
          f"--budget {a.budget:g} --grids {best.grids} "
          f"--range-pct {best.range_pct:.4f}")

    print("""
CAVEATS — read these before believing the ranking
  * 'est $/d' assumes price oscillates through the grid. In a trend the grid
    stops trading and holds a losing position instead. The backtest in the
    README lost 14.66% in a sustained downtrend while completing ZERO round
    trips. This column does not model that at all.
  * Pairs that rank high do so by being volatile. Volatile pairs are also the
    ones that blow through a grid's range and leave you holding the bag.
  * Realised volatility is backward-looking. A pair that ranged 4%/day last
    week can trend 20% tomorrow.
  * Thin books fill worse than the mid price implies, and 24h volume flatters
    pairs whose liquidity is concentrated in a few minutes of the day.
  * Screening picks a pair that CAN work mechanically. It says nothing about
    whether that pair's price is likely to chop rather than trend, which is
    the only thing that actually determines whether the grid makes money.
""")


if __name__ == "__main__":
    main()
