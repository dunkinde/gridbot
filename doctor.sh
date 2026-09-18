#!/bin/bash
# Grid bot doctor: diagnose and auto-fix the common setup failures.
# Safe to run repeatedly. Prints a short report -- paste the whole thing.
#   bash doctor.sh
BOT=~/bot
ENVF=/etc/gridbot/env
echo "===== GRIDBOT DOCTOR ====="
date -u '+utc %Y-%m-%d %H:%M'

# ---- 1. machine ----------------------------------------------------------
mem=$(free -m | awk '/^Mem:/{print $2"MB total, "$7"MB avail"}')
swp=$(free -m | awk '/^Swap:/{print $2"MB"}')
echo "host      $mem | swap ${swp}"
[ "${swp%MB}" -eq 0 ] 2>/dev/null && echo "  WARN    no swap on a small box -- ccxt can get OOM-killed"
oom=$(sudo dmesg 2>/dev/null | grep -ci "killed process" || echo 0)
echo "oom kills $oom"
echo "disk      $(df -h / | awk 'NR==2{print $4" free"}')"

# ---- 2. files ------------------------------------------------------------
echo "--- files ---"
for f in trading_bot.py grid_strategy.py; do
  [ -f "$BOT/$f" ] && echo "  ok      $f" || echo "  MISSING $f"
done
[ -d "$BOT/.venv" ] && echo "  ok      .venv" || echo "  MISSING .venv"

# ---- 3. env file ---------------------------------------------------------
echo "--- credentials ---"
if ! sudo test -r "$ENVF"; then
  echo "  MISSING $ENVF"
else
  if sudo grep -qU $'\r' "$ENVF" 2>/dev/null; then
    echo "  FIXING  carriage returns found (Windows line endings) -- stripping"
    sudo sed -i 's/\r$//' "$ENVF"
  fi
  if sudo grep -qE '=[[:space:]]|[[:space:]]$' "$ENVF" 2>/dev/null; then
    echo "  FIXING  stray whitespace around values -- stripping"
    sudo sed -i -E 's/^([A-Z_]+)=[[:space:]]*(.*[^[:space:]])[[:space:]]*$/\1=\2/' "$ENVF"
  fi
  if sudo grep -qE '="|='"'" "$ENVF" 2>/dev/null; then
    echo "  note    values are quoted (usually fine, but unquoted is safer)"
  fi
  own=$(sudo stat -c '%U:%G %a' "$ENVF")
  echo "  owner   $own  (want ubuntu:ubuntu 600)"
  # report key shape WITHOUT printing the secret
  set -a; . "$ENVF" 2>/dev/null; set +a
  k="${EXCHANGE_API_KEY:-}"; s="${EXCHANGE_API_SECRET:-}"
  echo "  key     ${#k} chars, starts ${k:0:4}…"
  echo "  secret  ${#s} chars"
  [ ${#k} -ne 64 ] && echo "  WARN    Binance keys are normally 64 chars"
  [ ${#s} -ne 64 ] && echo "  WARN    Binance secrets are normally 64 chars"
fi

# ---- 4. the fetchCurrencies patch ---------------------------------------
echo "--- code patch ---"
if grep -q "fetchCurrencies" "$BOT/trading_bot.py" 2>/dev/null; then
  echo "  ok      fetchCurrencies guard present"
else
  echo "  FIXING  applying fetchCurrencies patch"
  sed -i '/"defaultType": "spot",/a\            "fetchCurrencies": False,' "$BOT/trading_bot.py"
  grep -q "fetchCurrencies" "$BOT/trading_bot.py" \
    && echo "  ok      patch applied" || echo "  FAILED  anchor not found -- tell Claude"
fi

# ---- 5. python -----------------------------------------------------------
echo "--- python ---"
# shellcheck disable=SC1091
source "$BOT/.venv/bin/activate" 2>/dev/null
python - <<'PY' 2>&1 | sed 's/^/  /'
import ast, sys
try:
    import ccxt; print(f"ok      ccxt {ccxt.__version__}")
except Exception as e:
    print("MISSING ccxt:", e); sys.exit()
for f in ("/home/ubuntu/bot/trading_bot.py", "/home/ubuntu/bot/grid_strategy.py"):
    try:
        ast.parse(open(f).read()); print(f"ok      syntax {f.split('/')[-1]}")
    except Exception as e:
        print(f"BROKEN  {f.split('/')[-1]}: {e}")
PY

# ---- 6. live auth test ---------------------------------------------------
echo "--- testnet auth ---"
python - <<'PY' 2>&1 | sed 's/^/  /'
import os, ccxt
try:
    ex = ccxt.binance({'apiKey': os.environ.get('EXCHANGE_API_KEY',''),
                       'secret': os.environ.get('EXCHANGE_API_SECRET',''),
                       'options': {'adjustForTimeDifference': True,
                                   'fetchCurrencies': False,
                                   'defaultType': 'spot'}})
    ex.set_sandbox_mode(True)
    api = ex.urls['api']
    host = api.get('private') if isinstance(api, dict) else api
    print("host   ", host)
    b = ex.fetch_balance()
    print(f"ok      auth works | USDT {b['total'].get('USDT')} BTC {b['total'].get('BTC')}")
    o = ex.fetch_open_orders('BTC/USDT')
    print(f"ok      {len(o)} open order(s) on BTC/USDT")
except Exception as e:
    print("FAILED ", type(e).__name__, str(e)[:160])
PY

# ---- 7. service ----------------------------------------------------------
echo "--- service ---"
if systemctl cat gridbot.service >/dev/null 2>&1; then
  echo "  ok      unit exists"
  systemctl is-active --quiet gridbot && echo "  ok      running" || echo "  STOPPED not running"
  echo "  restarts $(systemctl show gridbot -p NRestarts --value)"
  echo "  last error:"
  journalctl -u gridbot --no-pager 2>/dev/null \
    | grep -oE "(AuthenticationError|InsufficientFunds|NetworkError|ExchangeError|SystemExit|OSError|MemoryError)[^\"]{0,90}" \
    | tail -2 | sed 's/^/    /' || echo "    (none)"
else
  echo "  MISSING gridbot.service"
fi
echo "===== END ====="
