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
                 allow_same_candle_roundtrip: bool = False,
                 fill_through_pct: float = 0.0005,
                 queue_factor: float = 2.0,
                 gamma: float = 0.0,
                 trend_window: int = 0,
                 trend_band: float = 0.0):
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
        # A resting limit order does NOT fill just because price touched its
        # level -- you are in a queue behind everyone else who wanted that
        # price, and a brief touch that reverses leaves you unfilled. Requiring
        # price to trade THROUGH the level by this fraction is a crude proxy for
        # queue position. 0.0 reproduces the old, flattering behaviour.
        self.fill_through_pct = fill_through_pct

        # --- volume-based queue model (used by on_trade) -------------------
        # A resting limit order is not filled by price touching its level. It
        # fills when enough volume actually TRADES at or through that level to
        # clear the queue ahead of it. We cannot see the real queue, so we
        # require `queue_factor` times our own order size to trade at or
        # through the level before counting a fill. queue_factor=1.0 assumes we
        # are at the very front; 2.0 assumes an equal-sized order ahead of us.
        self.queue_factor = queue_factor
        self.pending_vol: List[float] = [0.0] * cfg.grids
        # A resting BUY can only exist below the market. A cell whose level is
        # above the current price has no order there -- in live trading
        # bootstrap() only arms cells with levels[i] < price. So a cell becomes
        # "armed" only once price has traded ABOVE its buy level; until then it
        # cannot fill. Without this the simulator buys at prices above market,
        # which no limit order could ever do, and paper stops being comparable
        # to live.
        self.armed: List[bool] = [False] * cfg.grids

        # --- inventory skew (Avellaneda-Stoikov reservation price) ---------
        # A plain grid quotes around a FIXED ladder, so a falling market fills
        # every buy on the way down and the position only grows. Real market
        # makers quote around a reservation price shifted away from mid by
        # their inventory: long inventory pushes quotes DOWN, so you stop
        # buying so eagerly and exit sooner.
        #
        #   shift = -gamma * (base value / budget) * band width
        #
        # gamma = 0.0 reproduces the naive symmetric grid (current behaviour).
        # gamma ~ 0.2 shifts the whole ladder down by a fifth of the band when
        # fully long. The trade-off is explicit: less profit in chop, smaller
        # drawdown in a trend. Which dominates is an empirical question -- run
        # both and measure.
        self.gamma = gamma
        self._band = cfg.upper - cfg.lower

        # --- trend filter --------------------------------------------------
        # A grid is short trend: it grinds out small wins sideways and is run
        # over in a sustained move, because it keeps buying all the way down.
        # Skewing quotes does not fix that (measured: it costs far more in chop
        # than it saves in a trend). Not being long does.
        #
        # So: track an EMA of price and stop ARMING BUYS while price is below
        # it by more than `trend_band`. Existing inventory still sells, so the
        # grid winds itself down to cash in a falling market and re-arms when
        # price recovers. trend_window = 0 disables the filter entirely.
        self.trend_window = trend_window
        self.trend_band = trend_band
        self._ema: Optional[float] = None
        self._alpha = 2.0 / (trend_window + 1.0) if trend_window > 0 else 0.0
        self.buys_blocked = 0
        self.trades_seen = 0
        self.volume_seen = 0.0

    def step(self, high: float, low: float, close: float) -> list:
        """
        Process one candle, return a list of fill events.

        Intrabar path is unknowable from OHLC alone. The conservative assumption
        used here is: within one candle a cell may buy OR sell, not both, unless
        allow_same_candle_roundtrip=True. Enabling it flatters the backtest.
        """
        cfg = self.cfg
        events = []
        self.feed_trend(close)
        may_buy = self.buying_allowed(close)
        lv = self.levels_now(close)
        for i in range(cfg.grids):
            bp, sp = lv[i], lv[i + 1]
            acted = False
            # price must trade through, not merely touch
            buy_trigger = bp * (1.0 - self.fill_through_pct)
            sell_trigger = sp * (1.0 + self.fill_through_pct)

            if (may_buy and self.holdings[i] == 0.0 and low <= buy_trigger
                    and self.cash >= cfg.per_cell):
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

            if (self.holdings[i] > 0.0 and high >= sell_trigger
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

    def feed_trend(self, price: float) -> None:
        """
        Advance the trend EMA by ONE time step.

        Call this once per bar, NOT once per trade. on_trade() fires thousands
        of times a minute on BTC/USDT, so ticking the EMA there would make a
        50-"sample" window span a few seconds and filter nothing. The candle
        path (step) ticks it itself, since there a bar IS the time unit; the
        trades path must be fed separately by the runner, once a minute.
        """
        if self.trend_window <= 0:
            return
        self._ema = price if self._ema is None else \
            self._alpha * price + (1 - self._alpha) * self._ema

    def buying_allowed(self, price: float) -> bool:
        """False while price sits below its EMA by more than trend_band."""
        if self.trend_window <= 0 or self._ema is None:
            return True
        return price >= self._ema * (1.0 - self.trend_band)

    def skew(self, price: float) -> float:
        """Price shift applied to every level, from current inventory. <= 0."""
        if self.gamma <= 0.0:
            return 0.0
        inv_frac = (self.base_held() * price) / self.cfg.budget if self.cfg.budget else 0.0
        inv_frac = max(0.0, min(inv_frac, 1.0))
        return -self.gamma * inv_frac * self._band

    def levels_now(self, price: float):
        """The ladder as it currently stands, after inventory skew."""
        sh = self.skew(price)
        return [lv + sh for lv in self.cfg.levels]

    def on_trade(self, price: float, qty: float) -> list:
        """
        Process ONE executed market trade from the venue's public tape.

        This is the realistic fill path: instead of assuming a fill because a
        candle's low reached our level, we accumulate the volume that actually
        traded at or through each resting order and fill only once enough has
        gone through to plausibly clear the queue ahead of us.

        Assumption worth knowing: the accumulator is NOT reset when price moves
        away from a level and later returns. A real queue does partially reset
        as orders are cancelled and added, so this is mildly optimistic --
        `queue_factor` is the dial that offsets it.
        """
        cfg = self.cfg
        events = []
        self.trades_seen += 1
        self.volume_seen += qty
        may_buy = self.buying_allowed(price)
        lv = self.levels_now(price)

        for i in range(cfg.grids):
            bp, sp = lv[i], lv[i + 1]

            if self.holdings[i] == 0.0:
                # a buy is resting at bp; only trades at or below it count
                if price > bp:
                    self.armed[i] = True      # market is above us: order can rest
                    continue
                if not may_buy:
                    self.buys_blocked += 1
                    continue
                if not self.armed[i] or self.cash < cfg.per_cell:
                    continue
                want = cfg.per_cell / bp
                self.pending_vol[i] += qty
                if self.pending_vol[i] < self.queue_factor * want:
                    continue
                got = cfg.per_cell * (1 - cfg.fee) / bp
                self.cash -= cfg.per_cell
                self.holdings[i] = got
                self.cost_basis[i] = cfg.per_cell
                self.fees_paid += cfg.per_cell * cfg.fee
                self.buys += 1
                self.pending_vol[i] = 0.0
                events.append({"side": "buy", "price": float(bp),
                               "qty": float(got), "cell": i})
            else:
                # a sell is resting at sp; only trades at or above it count
                if price < sp:
                    continue
                held = self.holdings[i]
                self.pending_vol[i] += qty
                if self.pending_vol[i] < self.queue_factor * held:
                    continue
                proceeds = held * sp
                fee = proceeds * cfg.fee
                self.cash += proceeds - fee
                self.fees_paid += fee
                self.realized_pnl += proceeds - fee - self.cost_basis[i]
                self.holdings[i] = 0.0
                self.cost_basis[i] = 0.0
                self.sells += 1
                self.completed += 1
                self.pending_vol[i] = 0.0
                events.append({"side": "sell", "price": float(sp),
                               "qty": float(held), "cell": i})
        return events

    # ---- persistence -----------------------------------------------------
    def to_dict(self) -> dict:
        """Everything needed to resume this simulation exactly."""
        return {
            "cash": self.cash,
            "holdings": list(self.holdings),
            "cost_basis": list(self.cost_basis),
            "pending_vol": list(self.pending_vol),
            "armed": list(self.armed),
            "buys": self.buys,
            "sells": self.sells,
            "completed": self.completed,
            "fees_paid": self.fees_paid,
            "realized_pnl": self.realized_pnl,
            "trades_seen": self.trades_seen,
            "volume_seen": self.volume_seen,
            "queue_factor": self.queue_factor,
            "gamma": self.gamma,
            "trend_window": self.trend_window,
            "trend_band": self.trend_band,
            "ema": self._ema,
            "fill_through_pct": self.fill_through_pct,
        }

    def load_dict(self, d: dict) -> None:
        """Restore from to_dict(). The GridConfig must already match."""
        n = self.cfg.grids
        def fit(seq, default):
            seq = list(seq or [])
            return (seq + [default] * n)[:n]
        self.cash = float(d.get("cash", self.cash))
        self.holdings = [float(x) for x in fit(d.get("holdings"), 0.0)]
        self.cost_basis = [float(x) for x in fit(d.get("cost_basis"), 0.0)]
        self.pending_vol = [float(x) for x in fit(d.get("pending_vol"), 0.0)]
        self.armed = [bool(x) for x in fit(d.get("armed"), False)]
        self.buys = int(d.get("buys", 0))
        self.sells = int(d.get("sells", 0))
        self.completed = int(d.get("completed", 0))
        self.fees_paid = float(d.get("fees_paid", 0.0))
        self.realized_pnl = float(d.get("realized_pnl", 0.0))
        self.trades_seen = int(d.get("trades_seen", 0))
        self.volume_seen = float(d.get("volume_seen", 0.0))
        if d.get("ema") is not None:
            self._ema = float(d["ema"])

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
            "trades_observed": self.trades_seen,
            "volume_observed": self.volume_seen,
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
