#!/bin/bash
# Creates helper commands in ~/bin for the grid bot.
# Run once:  bash install_scripts.sh
set -e
mkdir -p "$HOME/bin"

# ---- shared environment loader -------------------------------------------
cat > "$HOME/bin/_botenv" <<'EOF'
# sourced by the bot-* commands
cd "$HOME/bot" || { echo "no ~/bot directory"; exit 1; }
# shellcheck disable=SC1091
source .venv/bin/activate
set -a
# shellcheck disable=SC1091
source /etc/gridbot/env
set +a
SYMBOL="${SYMBOL:-BTC/USDT}"
EOF

# ---- bot-orders : what is resting on the exchange ------------------------
cat > "$HOME/bin/bot-orders" <<'EOF'
#!/bin/bash
source "$HOME/bin/_botenv"
python - "$SYMBOL" <<'PY'
import ccxt, os, sys
sym = sys.argv[1]
ex = ccxt.binance({'apiKey': os.environ['EXCHANGE_API_KEY'],
                   'secret': os.environ['EXCHANGE_API_SECRET'],
                   'options': {'adjustForTimeDifference': True}})
ex.set_sandbox_mode(os.environ.get('BOT_LIVE') != '1')
orders = ex.fetch_open_orders(sym)
print(f"{len(orders)} open order(s) on {sym}")
for o in sorted(orders, key=lambda x: x['price'] or 0):
    print(f"  {o['side']:<4} {o['amount']:.8f} @ {o['price']:>12,.2f}")
PY
EOF

# ---- bot-count : just the number (used by bot-crashtest) -----------------
cat > "$HOME/bin/bot-count" <<'EOF'
#!/bin/bash
source "$HOME/bin/_botenv"
python - "$SYMBOL" <<'PY'
import ccxt, os, sys
ex = ccxt.binance({'apiKey': os.environ['EXCHANGE_API_KEY'],
                   'secret': os.environ['EXCHANGE_API_SECRET'],
                   'options': {'adjustForTimeDifference': True}})
ex.set_sandbox_mode(os.environ.get('BOT_LIVE') != '1')
print(len(ex.fetch_open_orders(sys.argv[1])))
PY
EOF

# ---- bot-balance ---------------------------------------------------------
cat > "$HOME/bin/bot-balance" <<'EOF'
#!/bin/bash
source "$HOME/bin/_botenv"
python <<'PY'
import ccxt, os
ex = ccxt.binance({'apiKey': os.environ['EXCHANGE_API_KEY'],
                   'secret': os.environ['EXCHANGE_API_SECRET'],
                   'options': {'adjustForTimeDifference': True}})
ex.set_sandbox_mode(os.environ.get('BOT_LIVE') != '1')
b = ex.fetch_balance()
rows = [(k, v) for k, v in b['total'].items() if v]
rows.sort(key=lambda r: -r[1])
for k, v in rows[:12]:
    free = b['free'].get(k, 0)
    print(f"  {k:<8} total {v:>16,.8f}   free {free:>16,.8f}")
PY
EOF

# ---- bot-price : testnet vs production -----------------------------------
cat > "$HOME/bin/bot-price" <<'EOF'
#!/bin/bash
source "$HOME/bin/_botenv"
python - "$SYMBOL" <<'PY'
import ccxt, sys
sym = sys.argv[1]
prod = ccxt.binance()
test = ccxt.binance(); test.set_sandbox_mode(True)
try:
    p = prod.fetch_ticker(sym)['last']
    t = test.fetch_ticker(sym)['last']
    print(f"production {p:>12,.2f} | testnet {t:>12,.2f} | diff {(t-p)/p*100:+.2f}%")
except Exception as e:
    print("price check failed:", e)
PY
EOF

# ---- bot-state : what the bot thinks it is holding ------------------------
cat > "$HOME/bin/bot-state" <<'EOF'
#!/bin/bash
source "$HOME/bin/_botenv"
F="$HOME/bot/state.json"
[ -f "$F" ] || { echo "no state file yet at $F"; exit 0; }
python - "$F" <<'PY'
import json, sys, time
st = json.load(open(sys.argv[1]))
g = st['grid']
print(f"symbol        {st['symbol']}  ({st['mode']})")
print(f"range         {g['lower']:,.2f} .. {g['upper']:,.2f}  ({g['grids']} cells)")
print(f"round trips   {st['roundtrips']}")
print(f"quote spent   {st['quote_spent']:,.2f}")
print(f"quote recvd   {st['quote_received']:,.2f}")
print(f"uptime        {(time.time()-st['started_at'])/3600:,.1f} h")
print(f"saved         {time.strftime('%H:%M:%S', time.localtime(st['saved_at']))}")
print("cells:")
for i, c in sorted(st['cells'].items(), key=lambda kv: int(kv[0])):
    mark = "" if c['status'] == 'idle' else "  <-"
    print(f"  {int(i):>2} {c['status']:<10} qty={c['qty']:.8f}{mark}")
PY
EOF

# ---- bot-status : the daily check ----------------------------------------
cat > "$HOME/bin/bot-status" <<'EOF'
#!/bin/bash
echo "=== service ==="
systemctl is-active gridbot >/dev/null 2>&1 && echo "  running" || echo "  NOT RUNNING"
systemctl show gridbot -p NRestarts --value 2>/dev/null | sed 's/^/  restarts: /'
echo
echo "=== state ==="
bot-state
echo
echo "=== exchange ==="
bot-orders
echo
bot-price
echo
echo "=== last 24h problems ==="
n=$(journalctl -u gridbot --since "24 hours ago" --no-pager 2>/dev/null | grep -ciE "error|traceback|critical" || true)
echo "  $n error line(s)"
[ "$n" -gt 0 ] && journalctl -u gridbot --since "24 hours ago" --no-pager | grep -iE "error|traceback|critical" | tail -5
exit 0
EOF

# ---- bot-logs / bot-errors -----------------------------------------------
cat > "$HOME/bin/bot-logs" <<'EOF'
#!/bin/bash
journalctl -u gridbot -f -n "${1:-40}"
EOF

cat > "$HOME/bin/bot-errors" <<'EOF'
#!/bin/bash
journalctl -u gridbot --since "${1:-24 hours ago}" --no-pager \
  | grep -iE "error|traceback|critical|warning" | tail -40
EOF

cat > "$HOME/bin/bot-fills" <<'EOF'
#!/bin/bash
journalctl -u gridbot --since "${1:-24 hours ago}" --no-pager \
  | grep -E "FILLED|filled" | tail -40
EOF

# ---- service control -----------------------------------------------------
cat > "$HOME/bin/bot-start" <<'EOF'
#!/bin/bash
sudo systemctl start gridbot && echo "started" && sleep 3 && journalctl -u gridbot -n 15 --no-pager
EOF

cat > "$HOME/bin/bot-stop" <<'EOF'
#!/bin/bash
echo "stopping (orders should be cancelled on the way out)..."
sudo systemctl stop gridbot
sleep 3
bot-orders
EOF

cat > "$HOME/bin/bot-restart" <<'EOF'
#!/bin/bash
sudo systemctl restart gridbot && echo "restarted" && sleep 5 && journalctl -u gridbot -n 20 --no-pager
EOF

# ---- bot-panic : cancel everything ---------------------------------------
cat > "$HOME/bin/bot-panic" <<'EOF'
#!/bin/bash
read -rp "Stop the bot and cancel ALL open orders? [y/N] " a
[ "$a" = "y" ] || { echo "aborted"; exit 0; }
sudo systemctl stop gridbot
source "$HOME/bin/_botenv"
python - "$SYMBOL" <<'PY'
import ccxt, os, sys
ex = ccxt.binance({'apiKey': os.environ['EXCHANGE_API_KEY'],
                   'secret': os.environ['EXCHANGE_API_SECRET'],
                   'options': {'adjustForTimeDifference': True}})
ex.set_sandbox_mode(os.environ.get('BOT_LIVE') != '1')
n = 0
for o in ex.fetch_open_orders(sys.argv[1]):
    ex.cancel_order(o['id'], sys.argv[1]); n += 1
print(f"cancelled {n} order(s)")
PY
EOF

# ---- bot-crashtest : checklist item 4 -------------------------------------
cat > "$HOME/bin/bot-crashtest" <<'EOF'
#!/bin/bash
# Simulates a hard crash and verifies the ladder is NOT duplicated.
echo "counting orders before..."
before=$(bot-count) || { echo "count failed"; exit 1; }
echo "  before: $before"
echo "killing the bot with SIGKILL (no clean shutdown)..."
sudo systemctl kill -s SIGKILL gridbot
echo "waiting 30s for systemd to restart it..."
sleep 30
echo "counting orders after..."
after=$(bot-count) || { echo "count failed"; exit 1; }
echo "  after:  $after"
echo
if [ "$before" = "$after" ]; then
  echo "PASS - order count unchanged ($before), no duplicate ladder"
else
  echo "FAIL - count changed $before -> $after. Investigate before going live."
fi
echo
journalctl -u gridbot -n 25 --no-pager | grep -iE "resumed|reconcil|orphan|vanish" || \
  echo "(no reconcile lines in the last 25 log lines)"
EOF

# ---- bot-update : pull new code from GitHub -------------------------------
cat > "$HOME/bin/bot-update" <<'EOF'
#!/bin/bash
REPO="${BOT_REPO:-https://github.com/dunkinde/gridbot.git}"
rm -rf /tmp/gridbot
git clone -q "$REPO" /tmp/gridbot || { echo "clone failed"; exit 1; }
cp /tmp/gridbot/*.py "$HOME/bot/" && echo "code updated from $REPO"
ls -la "$HOME/bot"/*.py
echo "run 'bot-restart' to pick up the change"
EOF

chmod +x "$HOME/bin/"bot-* 
echo 'export PATH="$HOME/bin:$PATH"' >> "$HOME/.bashrc"
export PATH="$HOME/bin:$PATH"

echo
echo "installed into ~/bin:"
ls -1 "$HOME/bin" | sed 's/^/  /'
echo
echo "PATH updated. Commands are available now in this shell,"
echo "and in every new shell from here on."
