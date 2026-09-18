#!/usr/bin/env python3
"""
Grid Trading Bot — paper / testnet / live, with crash-safe state.

  python trading_bot.py --mode paper                 # sim money, REAL market data
  python trading_bot.py --mode testnet                # fake money, real order flow
  python trading_bot.py --mode live --budget 50       # real money. start tiny.
  python trading_bot.py --mode live --cancel-all      # panic button

Environment (never put keys in this file):
  EXCHANGE_API_KEY, EXCHANGE_API_SECRET, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

SAFETY
  - API key must have Spot Trading + IP allowlist, and NO withdrawal permission.
  - Live mode refuses to start unless --i-understand-the-risk is passed.
  - Orders are cancelled on shutdown (SIGINT/SIGTERM) and on the kill switch.
  - Grid bots are short trend: they grind out small wins sideways and bleed in a
    sustained move. The drawdown kill switch is not optional decoration.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import queue
import signal
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Dict, Optional

try:
    import ccxt
except ImportError:
    raise SystemExit("pip install ccxt")

from grid_strategy import GridSim, make_grid_around

log = logging.getLogger("gridbot")

# Not every ccxt build exports this; fall back rather than blow up at import.
OrderRejected = getattr(ccxt, "OrderImmediatelyFillable", ccxt.InvalidOrder)

STATE_VERSION = 2


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
@dataclass
class Config:
    exchange: str = "binance"
    symbol: str = "BTC/USDT"
    mode: str = "paper"              # paper | testnet | live
    budget: float = 500.0
    range_pct: float = 0.03
    grids: int = 12
    fee: float = 0.001               # 0.00075 if you pay fees in BNB
    timeframe: str = "1m"
    poll_seconds: int = 15
    rebuild_on_drift: bool = True    # paper only; live pauses instead
    seed_inventory: bool = False     # live: market-buy base for cells above price
    max_drawdown_pct: float = 0.15   # kill switch on grid-scoped PnL
    post_only: bool = True
    state_file: str = "gridbot_state.json"
    escape_tolerance: float = 0.005

    @property
    def paper(self) -> bool:
        return self.mode == "paper"

    @property
    def testnet(self) -> bool:
        # Paper mode must use REAL market data. Testnet candles are thin and
        # gappy; a paper run fed testnet OHLCV tells you nothing.
        return self.mode == "testnet"


# --------------------------------------------------------------------------
# Telegram — off the hot path
# --------------------------------------------------------------------------
class Notifier(threading.Thread):
    """
    The original called urlopen(timeout=10) inline, so a slow Telegram stalled
    fill detection for up to 10s per message. This drains a queue on a daemon
    thread instead; the trading loop never blocks on it.
    """

    def __init__(self, token: str, chat: str):
        super().__init__(daemon=True, name="notifier")
        self.token, self.chat = token, chat
        self.q: queue.Queue = queue.Queue(maxsize=200)
        self.enabled = bool(token and chat)
        self._stop = threading.Event()
        if self.enabled:
            self.start()

    def send(self, msg: str) -> None:
        log.info("NOTIFY %s", msg.replace("\n", " | "))
        if not self.enabled:
            return
        try:
            self.q.put_nowait(msg)
        except queue.Full:
            log.warning("notify queue full, dropping message")

    def run(self) -> None:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        while not self._stop.is_set():
            try:
                msg = self.q.get(timeout=1.0)
            except queue.Empty:
                continue
            for attempt in range(3):
                try:
                    body = json.dumps({"chat_id": self.chat, "text": f"🤖 {msg}"}).encode()
                    req = urllib.request.Request(
                        url, data=body, headers={"Content-Type": "application/json"})
                    urllib.request.urlopen(req, timeout=10)
                    break
                except Exception as e:
                    if attempt == 2:
                        log.error("telegram failed after retries: %s", e)
                    else:
                        time.sleep(2 ** attempt)

    def drain(self, timeout: float = 5.0) -> None:
        """Best-effort flush so shutdown messages actually get delivered."""
        if not self.enabled:
            return
        deadline = time.time() + timeout
        while not self.q.empty() and time.time() < deadline:
            time.sleep(0.1)
        self._stop.set()


# --------------------------------------------------------------------------
# Exchange
# --------------------------------------------------------------------------
def make_exchange(cfg: Config) -> "ccxt.Exchange":
    klass = getattr(ccxt, cfg.exchange)
    ex = klass({
        "apiKey": os.getenv("EXCHANGE_API_KEY", ""),
        "secret": os.getenv("EXCHANGE_API_SECRET", ""),
        "enableRateLimit": True,
        "options": {
            # Binance rejects signed requests whose timestamp drifts outside
            # recvWindow (error -1021). Keep chrony running on the box AND let
            # ccxt correct for residual drift.
            "adjustForTimeDifference": True,
            "recvWindow": 5000,
            "defaultType": "spot",
            # load_markets() calls fetch_currencies(), which ccxt routes to
            # PRODUCTION's /sapi/v1/capital/config/getall even in sandbox mode.
            # The testnet has no /sapi at all, so testnet keys get rejected
            # there with -2008 "Invalid Api-Key ID" and the bot dies at startup.
            # Nothing here needs the currency list.
            "fetchCurrencies": False,
        },
    })
    if cfg.testnet:
        if not hasattr(ex, "set_sandbox_mode"):
            raise SystemExit(f"{cfg.exchange} has no testnet in ccxt")
        ex.set_sandbox_mode(True)
        log.info("TESTNET enabled — fake money, real order flow")
    ex.load_markets()
    return ex


@dataclass
class MarketRules:
    amount_step: float
    price_step: float
    min_amount: float
    min_cost: float

    @classmethod
    def load(cls, ex, symbol: str) -> "MarketRules":
        m = ex.market(symbol)
        prec = m.get("precision") or {}
        limits = m.get("limits") or {}

        def as_step(p, default):
            if p is None:
                return default
            p = float(p)
            # ccxt gives either a tick size (0.00001) or decimal places (5)
            return p if p < 1 else 10.0 ** (-int(p))

        return cls(
            amount_step=as_step(prec.get("amount"), 1e-8),
            price_step=as_step(prec.get("price"), 0.01),
            min_amount=float((limits.get("amount") or {}).get("min") or 0.0),
            min_cost=float((limits.get("cost") or {}).get("min") or 0.0),
        )

    def floor_amount(self, amt: float) -> float:
        """
        Always round DOWN. Rounding up is how you get
        'Account has insufficient balance for requested action'.
        """
        if self.amount_step <= 0:
            return amt
        return math.floor(amt / self.amount_step + 1e-9) * self.amount_step


def validate_grid(gcfg, rules: MarketRules, notifier: Notifier,
                  mode: str = "live") -> None:
    """
    Refuse to run a grid the exchange will reject order by order.

    Exchange-filter problems (min notional, lot step, zero qty) are fatal in
    every mode -- the testnet enforces the same filters as production, so an
    order that would be rejected live is rejected there too.

    Economic problems (lot-size rounding swamping the edge) are fatal only in
    LIVE mode. On paper/testnet you are testing plumbing, not profitability,
    and a deliberately tiny sandbox budget should not be blocked for being
    unprofitable -- it was never going to make money.
    """
    problems = []
    warnings = []
    if rules.min_cost and gcfg.per_cell < rules.min_cost:
        problems.append(
            f"per-cell notional {gcfg.per_cell:.2f} is below the exchange minimum "
            f"{rules.min_cost:.2f}. Raise budget to >= {rules.min_cost * gcfg.grids:.2f} "
            f"or cut grids to <= {int(gcfg.budget / rules.min_cost)}.")
    smallest_qty = gcfg.per_cell / gcfg.levels[-1]
    if rules.min_amount and smallest_qty < rules.min_amount:
        problems.append(f"smallest order qty {smallest_qty:.8f} < min {rules.min_amount:.8f}")
    if rules.floor_amount(smallest_qty) <= 0:
        problems.append(f"qty {smallest_qty:.10f} rounds to zero at step {rules.amount_step}")
    if gcfg.net_edge_pct() <= 0:
        problems.append(f"net edge after fees is {gcfg.net_edge_pct():.4%} — this grid "
                        f"cannot be profitable")

    # LOT_SIZE quantization. Every order amount must be a whole multiple of
    # amount_step, so each fill discards up to one step of base. In quote terms
    # that is (amount_step * price) per order, and it has to be small next to
    # the net edge per round trip -- otherwise rounding eats the strategy.
    # This is the check that rules out "$500 of BTC across 12 grids".
    quantum = rules.amount_step * gcfg.levels[-1]
    edge_quote = gcfg.per_cell * gcfg.net_edge_pct()
    if edge_quote > 0 and quantum > 0.25 * edge_quote:
        need_per_cell = quantum / (0.25 * gcfg.net_edge_pct())
        (problems if mode == "live" else warnings).append(
            f"lot-size rounding dominates the edge: one {rules.amount_step:g} step is "
            f"{quantum:,.2f} quote at this price, against a net edge of only "
            f"{edge_quote:,.4f} per round trip ({quantum / edge_quote:.1f}x). "
            f"Profitable sizing needs >= {need_per_cell:,.0f} per cell "
            f"(budget >= {need_per_cell * gcfg.grids:,.0f} at {gcfg.grids} grids), "
            f"or fewer grids, or a pair with a finer lot step.")

    for w in warnings:
        log.warning("SANDBOX SIZING: %s", w)
    if warnings:
        notifier.send("⚠️ Sandbox sizing: this grid is deliberately too small to be "
                      "profitable. Good for testing plumbing, not a strategy result.")
    if problems:
        msg = "Grid rejected:\n  - " + "\n  - ".join(problems)
        notifier.send("🛑 " + msg)
        raise SystemExit(msg)


# --------------------------------------------------------------------------
# Live grid
# --------------------------------------------------------------------------
IDLE, BUY_OPEN, HOLDING, SELL_OPEN = "idle", "buy_open", "holding", "sell_open"


class LiveGrid:
    """
    One cell = one [buy_level, sell_level] pair with an explicit state machine:

        idle --place buy--> buy_open --filled--> holding
          ^                                         |
          |                                    place sell
          +------------ filled <-- sell_open <------+

    The cell's state, its resting order id and the ACTUAL filled base quantity
    are persisted to disk after every transition, so a restart re-adopts the
    live orders instead of laying a second ladder on top of the first.
    """

    def __init__(self, ex, cfg: Config, gcfg, rules: MarketRules, notifier: Notifier):
        self.ex, self.cfg, self.gcfg, self.rules = ex, cfg, gcfg, rules
        self.notify = notifier
        self.symbol = cfg.symbol
        self.base, self.quote = cfg.symbol.split("/")
        self.cells: Dict[int, dict] = {
            i: {"status": IDLE, "order_id": None, "qty": 0.0, "cost": 0.0}
            for i in range(gcfg.grids)
        }
        self.quote_spent = 0.0
        self.quote_received = 0.0
        self.roundtrips = 0
        self.started_at = time.time()
        self._shutdown = threading.Event()

    # ---- persistence -----------------------------------------------------
    def save(self) -> None:
        payload = {
            "version": STATE_VERSION,
            "symbol": self.symbol,
            "mode": self.cfg.mode,
            "grid": {"lower": self.gcfg.lower, "upper": self.gcfg.upper,
                     "grids": self.gcfg.grids, "budget": self.gcfg.budget,
                     "levels": list(self.gcfg.levels)},
            "cells": self.cells,
            "quote_spent": self.quote_spent,
            "quote_received": self.quote_received,
            "roundtrips": self.roundtrips,
            "started_at": self.started_at,
            "saved_at": time.time(),
        }
        tmp = self.cfg.state_file + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(payload, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.cfg.state_file)   # atomic; never a half-written state

    def load(self) -> bool:
        if not os.path.exists(self.cfg.state_file):
            return False
        try:
            with open(self.cfg.state_file) as fh:
                st = json.load(fh)
        except Exception as e:
            log.error("state file unreadable (%s) — refusing to guess", e)
            raise SystemExit("Move or delete the corrupt state file, then reconcile "
                             "open orders manually on the exchange.")
        if st.get("version") != STATE_VERSION or st.get("symbol") != self.symbol:
            log.warning("state file is for a different version/symbol — ignoring")
            return False
        g = st["grid"]
        if abs(g["lower"] - self.gcfg.lower) / self.gcfg.lower > 1e-6:
            log.warning("saved grid range differs from the new one; adopting the SAVED "
                        "grid so resting orders still map to their cells")
            self.gcfg.lower, self.gcfg.upper = g["lower"], g["upper"]
            self.gcfg.levels = g["levels"]
        self.cells = {int(k): v for k, v in st["cells"].items()}
        self.quote_spent = st.get("quote_spent", 0.0)
        self.quote_received = st.get("quote_received", 0.0)
        self.roundtrips = st.get("roundtrips", 0)
        self.started_at = st.get("started_at", time.time())
        log.info("resumed state: %d cells, %d roundtrips", len(self.cells), self.roundtrips)
        return True

    # ---- order helpers ---------------------------------------------------
    def _place(self, side: str, price: float, cell: int, amount: float) -> Optional[str]:
        amount = self.rules.floor_amount(amount)
        price = float(self.ex.price_to_precision(self.symbol, price))
        if amount <= 0:
            log.error("cell %d: amount rounds to zero, skipping", cell)
            return None
        if self.rules.min_cost and amount * price < self.rules.min_cost:
            log.error("cell %d: notional %.2f below minimum %.2f, skipping",
                      cell, amount * price, self.rules.min_cost)
            return None
        params = {}
        if self.cfg.post_only:
            # On Binance spot this becomes LIMIT_MAKER. At VIP0 maker and taker
            # are both 0.10%, so this is not a fee saving — it is protection
            # against a re-placed order crossing the spread and filling at a
            # worse price than the grid arithmetic assumes.
            params["postOnly"] = True
        try:
            od = self.ex.create_order(self.symbol, "limit", side, amount, price, params)
        except OrderRejected:
            log.warning("cell %d: %s @ %.2f would cross the book; will retry next poll",
                        cell, side, price)
            return None
        except ccxt.InsufficientFunds as e:
            log.error("cell %d: insufficient funds for %s %.8f @ %.2f: %s",
                      cell, side, amount, price, e)
            return None
        log.info("placed %s %.8f @ %.2f (cell %d)", side, amount, price, cell)
        return od["id"]

    def _filled_base(self, od: dict) -> float:
        """
        Base actually received on a buy, net of fee.

        Binance charges the spot buy fee in the BASE asset unless you pay in BNB,
        so `filled` overstates what you can sell. Selling `filled` fails with
        insufficient balance; selling per_cell/level (the original bug) sells
        ~0.5% too little every cycle and slowly starves the grid.
        """
        filled = float(od.get("filled") or od.get("amount") or 0.0)
        fee = od.get("fee") or {}
        fees = od.get("fees") or ([fee] if fee else [])
        deduct = sum(float(f.get("cost") or 0.0) for f in fees
                     if (f.get("currency") or "").upper() == self.base.upper())
        return max(filled - deduct, 0.0)

    def _cost_quote(self, od: dict) -> float:
        cost = od.get("cost")
        if cost:
            return float(cost)
        px = float(od.get("average") or od.get("price") or 0.0)
        return px * float(od.get("filled") or 0.0)

    # ---- startup ---------------------------------------------------------
    def reconcile(self) -> None:
        """
        Make our recorded state and the exchange's reality agree before trading.
        This is the step whose absence makes `Restart=always` dangerous.
        """
        open_orders = self.ex.fetch_open_orders(self.symbol)
        live_ids = {o["id"]: o for o in open_orders}
        ours = {c["order_id"] for c in self.cells.values() if c["order_id"]}

        for i, cell in self.cells.items():
            oid = cell["order_id"]
            if not oid:
                continue
            if oid in live_ids:
                continue                       # still resting, nothing to do
            try:
                od = self.ex.fetch_order(oid, self.symbol)
            except ccxt.OrderNotFound:
                # The Spot Testnet wipes all open orders on its monthly reset,
                # and a human can always cancel from the web UI. Either way the
                # order is simply gone; reset the cell and let _repair re-arm it.
                log.warning("cell %d: order %s no longer exists — resetting cell", i, oid)
                cell.update(order_id=None,
                            status=HOLDING if cell["status"] == SELL_OPEN else IDLE)
                continue
            status = od.get("status")
            if status == "closed":
                log.info("cell %d: order %s filled while we were down", i, oid)
                self._on_fill(i, od, persist=False)
            else:
                log.info("cell %d: order %s is %s — resetting cell", i, oid, status)
                cell.update(order_id=None,
                            status=HOLDING if cell["status"] == SELL_OPEN else IDLE)

        orphans = [o for oid, o in live_ids.items() if oid not in ours]
        if orphans:
            log.warning("cancelling %d orphan order(s) not in our state", len(orphans))
            self.notify.send(f"⚠️ cancelling {len(orphans)} orphan order(s) on {self.symbol}")
            for o in orphans:
                try:
                    self.ex.cancel_order(o["id"], self.symbol)
                except Exception as e:
                    log.error("could not cancel orphan %s: %s", o["id"], e)
        self.save()

    def bootstrap(self, price: float) -> None:
        """Place the opening ladder. Buys below price; sells above IF seeded."""
        above = [i for i in range(self.gcfg.grids) if self.gcfg.levels[i] >= price]

        if self.cfg.seed_inventory and above:
            # A buy-only ladder (the original behaviour) cannot sell into a rally
            # until a buy has filled first, so an immediate move up earns nothing.
            need = sum(self.gcfg.per_cell / self.gcfg.levels[i + 1] for i in above)
            need = self.rules.floor_amount(need)
            if need > 0:
                log.info("seeding %.8f %s at market for %d cells above price",
                         need, self.base, len(above))
                od = self.ex.create_order(self.symbol, "market", "buy", need)
                got = self._filled_base(od)
                self.quote_spent += self._cost_quote(od)
                per = got / len(above)
                for i in above:
                    self.cells[i].update(status=HOLDING, qty=per,
                                         cost=self._cost_quote(od) / len(above))

        for i, cell in self.cells.items():
            if cell["status"] == IDLE and self.gcfg.levels[i] < price:
                oid = self._place("buy", self.gcfg.levels[i], i,
                                  self.gcfg.per_cell / self.gcfg.levels[i])
                if oid:
                    cell.update(status=BUY_OPEN, order_id=oid)
            elif cell["status"] == HOLDING:
                self._open_sell(i)
        self.save()

    def _open_sell(self, i: int) -> None:
        cell = self.cells[i]
        # Guard the top cell: levels has grids+1 entries, so cell i sells at i+1.
        # The original indexed levels[cell+1] unguarded and raised IndexError on
        # the top cell, which the bare except swallowed into an infinite retry.
        if i + 1 >= len(self.gcfg.levels):
            log.error("cell %d has no sell level above it", i)
            return
        sellable = self.rules.floor_amount(cell["qty"])
        px = self.gcfg.levels[i + 1]
        if sellable <= 0 or (self.rules.min_cost and sellable * px < self.rules.min_cost):
            log.warning("cell %d: position %.8f too small to sell (step %.8f) — "
                        "carrying it to the next cycle", i, cell["qty"], self.rules.amount_step)
            cell["status"] = IDLE
            return
        oid = self._place("sell", px, i, cell["qty"])
        if oid:
            cell.update(status=SELL_OPEN, order_id=oid)

    def _on_fill(self, i: int, od: dict, persist: bool = True) -> None:
        cell = self.cells[i]
        side = od.get("side")
        px = float(od.get("average") or od.get("price") or 0.0)
        if side == "buy":
            qty = self._filled_base(od)
            cost = self._cost_quote(od)
            self.quote_spent += cost
            cell.update(status=HOLDING, order_id=None,
                        qty=cell["qty"] + qty,        # + any carried dust
                        cost=cell["cost"] + cost)
            self.notify.send(f"BUY filled {self.symbol} {qty:.8f} @ {px:,.2f} (cell {i})")
            self._open_sell(i)
        else:
            proceeds = self._cost_quote(od)
            sold = float(od.get("filled") or od.get("amount") or 0.0)
            self.quote_received += proceeds
            self.roundtrips += 1
            # LOT_SIZE forces the sell to be a whole number of steps, so a little
            # base is always left behind. Carry it in the cell instead of zeroing
            # it: the remainder rides along with the next buy and gets sold once
            # it crosses a step boundary. Zeroing here would leak inventory every
            # cycle and slowly drain the quote side of the grid.
            qty_before = cell["qty"] or sold
            realised_cost = cell["cost"] * min(sold / qty_before, 1.0) if qty_before else 0.0
            pnl = proceeds - realised_cost
            cell.update(status=IDLE, order_id=None,
                        qty=max(qty_before - sold, 0.0),
                        cost=max(cell["cost"] - realised_cost, 0.0))
            self.notify.send(f"SELL filled {self.symbol} @ {px:,.2f} (cell {i}) "
                             f"+{pnl:,.2f} {self.quote} | {self.roundtrips} round trips")
            oid = self._place("buy", self.gcfg.levels[i], i,
                              self.gcfg.per_cell / self.gcfg.levels[i])
            if oid:
                cell.update(status=BUY_OPEN, order_id=oid)
        if persist:
            self.save()

    # ---- main loop -------------------------------------------------------
    def base_held(self) -> float:
        return sum(c["qty"] for c in self.cells.values())

    def pnl(self, price: float) -> float:
        """Grid-scoped PnL: realised quote flow plus the value of held inventory."""
        return self.quote_received - self.quote_spent + self.base_held() * price

    def poll(self) -> bool:
        """One iteration. Returns False when the bot should stop."""
        # ONE call to see every resting order, instead of one fetch_order per
        # cell per tick (12 calls -> 1 at the default grid size).
        live_ids = {o["id"] for o in self.ex.fetch_open_orders(self.symbol)}
        for i, cell in list(self.cells.items()):
            oid = cell["order_id"]
            if oid and oid not in live_ids:
                try:
                    od = self.ex.fetch_order(oid, self.symbol)
                except ccxt.OrderNotFound:
                    log.warning("cell %d: order %s vanished from the exchange", i, oid)
                    cell.update(order_id=None,
                                status=HOLDING if cell["status"] == SELL_OPEN else IDLE)
                    self.save()
                    continue
                if od.get("status") == "closed":
                    self._on_fill(i, od)
                else:
                    log.warning("cell %d order %s vanished as %s", i, oid, od.get("status"))
                    cell.update(order_id=None,
                                status=HOLDING if cell["status"] == SELL_OPEN else IDLE)
                    self.save()

        price = float(self.ex.fetch_ticker(self.symbol)["last"])
        self._repair(price)

        pnl = self.pnl(price)
        if pnl < -self.cfg.max_drawdown_pct * self.gcfg.budget:
            self.notify.send(f"🛑 KILL SWITCH: PnL {pnl:,.2f} exceeds "
                             f"{self.cfg.max_drawdown_pct:.0%} of budget. Cancelling all.")
            return False

        tol = self.cfg.escape_tolerance
        if price < self.gcfg.lower * (1 - tol) or price > self.gcfg.upper * (1 + tol):
            self.notify.send(f"⚠️ price {price:,.2f} escaped grid "
                             f"[{self.gcfg.lower:,.2f}, {self.gcfg.upper:,.2f}] — "
                             f"cancelling orders and stopping. Inventory held: "
                             f"{self.base_held():.8f} {self.base}")
            return False
        return True

    def _repair(self, price: float) -> None:
        """
        Re-place orders for cells that ended up with none — a postOnly rejection,
        a transient error, a cancelled order. Without this the grid silently
        shrinks: every cell that fails to re-arm is one that never trades again,
        and you would only notice weeks later from the fill count.
        """
        changed = False
        for i, cell in self.cells.items():
            if cell["order_id"]:
                continue
            if cell["status"] == IDLE and self.gcfg.levels[i] < price:
                oid = self._place("buy", self.gcfg.levels[i], i,
                                  self.gcfg.per_cell / self.gcfg.levels[i])
                if oid:
                    cell.update(status=BUY_OPEN, order_id=oid)
                    changed = True
            elif cell["status"] == HOLDING:
                self._open_sell(i)
                changed = True
        if changed:
            self.save()

    def cancel_all(self) -> None:
        """
        The original `return`ed on range escape, leaving every limit order live
        on the book to fill unattended. Pausing has to mean cancelling.
        """
        for i, cell in self.cells.items():
            oid = cell["order_id"]
            if not oid:
                continue
            try:
                self.ex.cancel_order(oid, self.symbol)
                log.info("cancelled cell %d order %s", i, oid)
            except ccxt.OrderNotFound:
                pass
            except Exception as e:
                log.error("cancel failed for %s: %s", oid, e)
            cell["order_id"] = None
            if cell["status"] == BUY_OPEN:
                cell["status"] = IDLE
            elif cell["status"] == SELL_OPEN:
                cell["status"] = HOLDING
        self.save()

    def summary(self, price: float) -> str:
        held = self.base_held()
        return (f"{self.symbol} @ {price:,.2f}\n"
                f"round trips: {self.roundtrips}\n"
                f"inventory:   {held:.8f} {self.base} ({held * price:,.2f} {self.quote})\n"
                f"PnL:         {self.pnl(price):,.2f} {self.quote}\n"
                f"uptime:      {(time.time() - self.started_at) / 3600:.1f} h")


# --------------------------------------------------------------------------
# Runners
# --------------------------------------------------------------------------
def run_paper(ex, cfg: Config, notifier: Notifier, stop: threading.Event) -> None:
    tf = cfg.timeframe
    ohlcv = ex.fetch_ohlcv(cfg.symbol, tf, limit=3)
    price = ohlcv[-1][4]
    gcfg = make_grid_around(price, cfg.budget, cfg.range_pct, cfg.grids, cfg.fee)
    sim = GridSim(gcfg)
    notifier.send(f"PAPER grid on {cfg.symbol} @ {price:,.2f}\n{gcfg.describe()}")
    log.info("\n%s", gcfg.describe())
    seen_ts = ohlcv[-1][0]
    backoff = cfg.poll_seconds

    while not stop.is_set():
        try:
            ohlcv = ex.fetch_ohlcv(cfg.symbol, tf, limit=5)
            for c in ohlcv:
                if c[0] <= seen_ts:
                    continue
                seen_ts = c[0]
                for ev in sim.step(c[2], c[3], c[4]):
                    log.info("PAPER %s cell=%d @ %.2f | equity=%.2f",
                             ev["side"], ev["cell"], ev["price"], sim.equity(c[4]))
            px = ohlcv[-1][4]
            if cfg.rebuild_on_drift and (px < gcfg.lower * (1 - cfg.escape_tolerance)
                                         or px > gcfg.upper * (1 + cfg.escape_tolerance)):
                st = sim.stats(px)
                notifier.send(f"⚠️ price {px:,.2f} escaped grid — re-centring.\n"
                              f"realised so far: {st['realized_pnl']:,.2f}, "
                              f"total PnL {st['total_pnl']:,.2f}")
                eq = sim.equity(px)
                gcfg = make_grid_around(px, eq, cfg.range_pct, cfg.grids, cfg.fee)
                sim = GridSim(gcfg, cash=eq)
                log.info("re-centred around %.2f", px)
            backoff = cfg.poll_seconds
        except ccxt.NetworkError as e:
            log.warning("network: %s (backoff %ds)", e, backoff)
            backoff = min(backoff * 2, 300)
        stop.wait(backoff)

    px = ex.fetch_ticker(cfg.symbol)["last"]
    notifier.send(f"PAPER stopped.\n{json.dumps(sim.stats(px), indent=2, default=float)}")


def run_live(ex, cfg: Config, notifier: Notifier, stop: threading.Event) -> None:
    price = float(ex.fetch_ticker(cfg.symbol)["last"])
    rules = MarketRules.load(ex, cfg.symbol)
    gcfg = make_grid_around(price, cfg.budget, cfg.range_pct, cfg.grids, cfg.fee)
    validate_grid(gcfg, rules, notifier, mode=cfg.mode)

    grid = LiveGrid(ex, cfg, gcfg, rules, notifier)
    resumed = grid.load()

    quote = cfg.symbol.split("/")[1]
    bal = ex.fetch_balance()
    free = float((bal.get(quote) or {}).get("free") or 0.0)   # no KeyError on a missing asset
    if free < gcfg.budget and not resumed:
        notifier.send(f"🛑 only {free:,.2f} {quote} free, grid needs {gcfg.budget:,.2f}")
        raise SystemExit("insufficient quote balance")

    log.info("\n%s", gcfg.describe())
    grid.reconcile()
    if not resumed:
        notifier.send(f"LIVE grid on {cfg.symbol} @ {price:,.2f}\n{gcfg.describe()}")
        grid.bootstrap(price)
    else:
        notifier.send(f"LIVE grid RESUMED on {cfg.symbol} @ {price:,.2f}\n"
                      f"{grid.summary(price)}")

    backoff = cfg.poll_seconds
    healthy = True
    while not stop.is_set() and healthy:
        try:
            healthy = grid.poll()
            backoff = cfg.poll_seconds
        except ccxt.AuthenticationError as e:
            # Bad key, or the IP allowlist does not include this box. Retrying
            # forever would just flood Telegram and earn a 418 IP ban.
            notifier.send(f"🛑 AUTH FAILED — check API key and IP allowlist: {e}")
            break
        except ccxt.InsufficientFunds as e:
            notifier.send(f"🛑 insufficient funds: {e}")
            break
        except ccxt.RateLimitExceeded as e:
            backoff = min(backoff * 2, 300)
            log.warning("rate limited: %s (backoff %ds)", e, backoff)
        except ccxt.NetworkError as e:
            backoff = min(backoff * 2, 300)
            log.warning("network: %s (backoff %ds)", e, backoff)
        except ccxt.ExchangeError as e:
            notifier.send(f"🛑 exchange error, stopping: {e}")
            break
        stop.wait(backoff)

    grid.cancel_all()
    try:
        px = float(ex.fetch_ticker(cfg.symbol)["last"])
        notifier.send("Bot stopped. All orders cancelled.\n" + grid.summary(px))
    except Exception:
        notifier.send("Bot stopped. All orders cancelled.")


# --------------------------------------------------------------------------
def parse_args() -> Config:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    d = Config()
    p.add_argument("--mode", choices=["paper", "testnet", "live"], default=d.mode)
    p.add_argument("--symbol", default=d.symbol)
    p.add_argument("--budget", type=float, default=d.budget)
    p.add_argument("--grids", type=int, default=d.grids)
    p.add_argument("--range-pct", type=float, default=d.range_pct)
    p.add_argument("--fee", type=float, default=d.fee,
                   help="per side. 0.00075 if you pay fees in BNB")
    p.add_argument("--poll-seconds", type=int, default=d.poll_seconds)
    p.add_argument("--max-drawdown-pct", type=float, default=d.max_drawdown_pct)
    p.add_argument("--state-file", default=d.state_file)
    p.add_argument("--seed-inventory", action="store_true",
                   help="market-buy base for cells above price so the grid can "
                        "sell into an immediate rally")
    p.add_argument("--no-post-only", action="store_true")
    p.add_argument("--cancel-all", action="store_true",
                   help="cancel every open order for the symbol and exit")
    p.add_argument("--i-understand-the-risk", action="store_true",
                   help="required for --mode live")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s")

    if a.mode == "live" and not a.i_understand_the_risk and not a.cancel_all:
        raise SystemExit(
            "Refusing to trade real money without --i-understand-the-risk.\n"
            "Run --mode paper for days, then --mode testnet, then live with a "
            "budget you would shrug off losing entirely.")

    cfg = Config(symbol=a.symbol, mode=a.mode, budget=a.budget, grids=a.grids,
                 range_pct=a.range_pct, fee=a.fee, poll_seconds=a.poll_seconds,
                 max_drawdown_pct=a.max_drawdown_pct, state_file=a.state_file,
                 seed_inventory=a.seed_inventory, post_only=not a.no_post_only)
    cfg._cancel_all = a.cancel_all      # type: ignore[attr-defined]
    return cfg


def main() -> None:
    cfg = parse_args()
    notifier = Notifier(os.getenv("TELEGRAM_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", ""))
    stop = threading.Event()

    def handle(signum, _frame):
        # systemd sends SIGTERM on stop/restart; without this the process dies
        # with its ladder still resting on the exchange.
        log.warning("signal %s received — shutting down", signum)
        stop.set()

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)

    ex = make_exchange(cfg)

    if getattr(cfg, "_cancel_all", False):
        n = 0
        for o in ex.fetch_open_orders(cfg.symbol):
            ex.cancel_order(o["id"], cfg.symbol)
            n += 1
        print(f"cancelled {n} open order(s) on {cfg.symbol}")
        return

    notifier.send(f"Bot starting | mode={cfg.mode} | {cfg.symbol} | budget {cfg.budget:,.2f}")
    try:
        (run_paper if cfg.paper else run_live)(ex, cfg, notifier, stop)
    except SystemExit:
        raise
    except Exception as e:
        log.exception("fatal")
        notifier.send(f"🛑 fatal: {e}")
        raise
    finally:
        notifier.drain()


if __name__ == "__main__":
    main()
