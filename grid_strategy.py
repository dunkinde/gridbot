"""
Grid-trading engine shared by the backtester, the paper simulator and the live bot.

Logic: split a price range [lower, upper] into N cells. Each cell i:
  - buys at levels[i]    (its lower edge)
  - sells at levels[i+1] (its upper edge)
Every completed buy->sell round trip earns one grid step, minus two lots of fees.

CHANGES FROM THE ORIGINAL
  * numpy dependency removed (pure Python; saves ~80 MB on a 1 GB VPS).
  * Geometric level spacing by default, so every cell has the SAME percentage
    step. Arithmetic (linspace) spacing gives fatter percentage steps at the
    bottom of the range and thinner ones at the top, which means the top cells
    can quietly be fee-negative while the guard rail looks at the average.
  * The fee guard now checks the WORST (smallest) step, not the mid-range step.
  * net_edge_pct() / describe() tell you the actual economics before you fund it.
  * Optional same-candle round trips, and a conservative intrabar fill model.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional


def _geometric_levels(lower: float, upper: float, n: int) -> List[float]:
    """Constant PERCENTAGE step between levels."""
    ratio = (upper / lower) ** (1.0 / n)
    return [lower * (ratio ** i) for i in range(n + 1)]


def _arithmetic_levels(lower: float, upper: float, n: int) -> List[float]:
    """Constant ABSOLUTE step between levels (the original np.linspace behaviour)."""
    step = (upper - lower) / n
    return [lower + step * i for i in range(n + 1)]


@dataclass
class GridConfig:
    budget: float
    lower: float                     # grid bottom price
    upper: float                     # grid top price
    grids: int                       # number of cells
    fee: float = 0.001               # per side. 0.001 = 0.1% Binance spot VIP0
                                     # 0.00075 with the BNB fee discount
    min_step_pct: Optional[float] = None   # None -> derived from fee (see below)
    fee_safety: float = 1.5          # require step >= 2*fee*fee_safety
    spacing: str = "geometric"       # "geometric" | "arithmetic"

    # computed in __post_init__
    levels: List[float] = field(default_factory=list, init=False, repr=False)
    per_cell: float = field(default=0.0, init=False)
    worst_step_pct: float = field(default=0.0, init=False)

    def __post_init__(self):
        if self.grids < 2:
            raise ValueError("Need at least 2 grids")
        if not (0 < self.lower < self.upper):
            raise ValueError(f"Invalid range [{self.lower}, {self.upper}]")
        if self.spacing not in ("geometric", "arithmetic"):
            raise ValueError("spacing must be 'geometric' or 'arithmetic'")

        builder = _geometric_levels if self.spacing == "geometric" else _arithmetic_levels
        self.levels = builder(self.lower, self.upper, self.grids)

        # Percentage step of each cell, measured against its own buy price —
        # that is what the round-trip return actually is.
        steps = [(self.levels[i + 1] - self.levels[i]) / self.levels[i]
                 for i in range(self.grids)]
        self.worst_step_pct = min(steps)

        required = (self.min_step_pct if self.min_step_pct is not None
                    else 2 * self.fee * self.fee_safety)
        if self.worst_step_pct < required:
            raise ValueError(
                f"Tightest grid step is {self.worst_step_pct:.3%}, need >= {required:.3%} "
                f"(round-trip fees alone are {2 * self.fee:.3%}). "
                f"Use fewer grids, a wider range, or lower fees (pay in BNB)."
            )
        self.per_cell = self.budget / self.grids

    # ---- economics -------------------------------------------------------
    def net_edge_pct(self) -> float:
        """Net return on one completed round trip, worst cell, after both fees."""
        return self.worst_step_pct - 2 * self.fee

    def net_profit_per_roundtrip(self) -> float:
        return self.per_cell * self.net_edge_pct()

    def describe(self) -> str:
        edge = self.net_edge_pct()
        per_rt = self.net_profit_per_roundtrip()
        lines = [
            f"range        [{self.lower:,.2f}, {self.upper:,.2f}]  ({self.spacing})",
            f"grids        {self.grids} cells, step {self.worst_step_pct:.4%} (tightest)",
            f"budget       {self.budget:,.2f}  ->  {self.per_cell:,.2f} per cell",
            f"fees         {self.fee:.4%} per side = {2 * self.fee:.4%} per round trip",
            f"net edge     {edge:.4%} per round trip = {per_rt:,.4f} per fill pair",
        ]
        if per_rt > 0:
            lines.append(f"break-even   {math.ceil(1 / edge / self.grids)} full grid "
                         f"sweeps to earn 1% on budget")
        return "\n".join(lines)


class GridSim:
    """State machine. Feed it OHLC candles one at a time; it returns fill events."""

    def __init__(self, cfg: GridConfig, cash: Optional[float] = None,
                 allow_same_candle_roundtrip: bool = False):
        self.cfg = cfg
        self.cash = cfg.budget if cash is None else cash
        self.holdings: List[float] = [0.0] * cfg.grids   # base qty held per cell
        self.cost_basis: List[float] = [0.0] * cfg.grids  # quote spent per cell
        self.buys = 0
        self.sells = 0
        self.completed = 0
        self.fees_paid = 0.0
        self.realized_pnl = 0.0
        self.allow_same_candle_roundtrip = allow_same_candle_roundtrip

    def step(self, high: float, low: float, close: float) -> list:
        """
        Process one candle, return a list of fill events.

        Intrabar path is unknowable from OHLC alone. The conservative assumption
        used here is: within one candle a cell may buy OR sell, not both, unless
        allow_same_candle_roundtrip=True. Enabling it flatters the backtest.
        """
        cfg = self.cfg
        events = []
        for i in range(cfg.grids):
            bp, sp = cfg.levels[i], cfg.levels[i + 1]
            acted = False

            if self.holdings[i] == 0.0 and low <= bp and self.cash >= cfg.per_cell:
                # Binance charges the spot buy fee in the BASE asset received.
                qty = cfg.per_cell * (1 - cfg.fee) / bp
                self.cash -= cfg.per_cell
                self.holdings[i] = qty
                self.cost_basis[i] = cfg.per_cell
                self.fees_paid += cfg.per_cell * cfg.fee
                self.buys += 1
                acted = True
                events.append({"side": "buy", "price": float(bp),
                               "qty": float(qty), "cell": i})

            if (self.holdings[i] > 0.0 and high >= sp
                    and (not acted or self.allow_same_candle_roundtrip)):
                qty = self.holdings[i]
                proceeds = qty * sp
                fee = proceeds * cfg.fee
                self.cash += proceeds - fee
                self.fees_paid += fee
                self.realized_pnl += proceeds - fee - self.cost_basis[i]
                self.holdings[i] = 0.0
                self.cost_basis[i] = 0.0
                self.sells += 1
                self.completed += 1
                events.append({"side": "sell", "price": float(sp),
                               "qty": float(qty), "cell": i})
        return events

    # ---- reporting -------------------------------------------------------
    def base_held(self) -> float:
        return sum(self.holdings)

    def equity(self, price: float) -> float:
        return self.cash + self.base_held() * price

    def stats(self, price: float) -> dict:
        eq = self.equity(price)
        return {
            "equity": eq,
            "cash": self.cash,
            "base_held": self.base_held(),
            "base_value": self.base_held() * price,
            "buys": self.buys,
            "sells": self.sells,
            "completed_roundtrips": self.completed,
            "realized_pnl": self.realized_pnl,
            "fees_paid": self.fees_paid,
            "total_pnl": eq - self.cfg.budget,
            "return_pct": (eq - self.cfg.budget) / self.cfg.budget,
        }


def make_grid_around(price: float, budget: float, range_pct: float = 0.03,
                     grids: int = 12, fee: float = 0.001,
                     spacing: str = "geometric") -> GridConfig:
    """Build a grid centred on `price`, spanning +/- range_pct."""
    return GridConfig(
        budget=budget,
        lower=price * (1 - range_pct),
        upper=price * (1 + range_pct),
        grids=grids,
        fee=fee,
        spacing=spacing,
    )


def make_grid_from_atr(price: float, budget: float, atr: float,
                       atr_mult: float = 2.0, grids: int = 12,
                       fee: float = 0.001) -> GridConfig:
    """
    Size the grid off recent realised volatility instead of a fixed percentage.

    `atr` is the Average True Range in PRICE units over your chosen lookback
    (7-day ATR on daily candles is a reasonable starting point for spot BTC).
    A grid narrower than the market's daily range re-centres constantly and
    realises a loss each time; one much wider than it never fills.
    """
    half = atr * atr_mult
    return GridConfig(budget=budget, lower=price - half, upper=price + half,
                      grids=grids, fee=fee, spacing="geometric")
