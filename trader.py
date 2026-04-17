"""
Hardin Trading Software v2 — One Big Trade Per Day
Dynamic position sizing and profit targets based on signal strength.
Agents learn from every trade and get smarter over time.
"""
import os, time, logging, json
from datetime import datetime, timedelta
import pytz
import pandas as pd
import alpaca_trade_api as tradeapi
from dotenv import load_dotenv
from scanner import scan_market, WATCHLIST
from agent_memory import AgentMemory

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('trader')

# ── Config ────────────────────────────────────────────────────────────────────
API_KEY    = os.getenv('ALPACA_API_KEY')
SECRET_KEY = os.getenv('ALPACA_SECRET_KEY')
BASE_URL   = os.getenv('ALPACA_BASE_URL', 'https://paper-api.alpaca.markets')
if BASE_URL and not BASE_URL.startswith('http'):
    BASE_URL = 'https://' + BASE_URL.lstrip('/')

MAX_DAILY_TRADES = 1   # ONE trade per day — go big or go home

# ── Dynamic scaling table ─────────────────────────────────────────────────────
# Score → (position_size, min_target_pct, max_target_pct, stop_loss_pct, tier_name)
SCALING = {
    # Score 6-7: cautious — small position
    6:  (20000, 1.0, 1.5, 0.5, 'CAUTIOUS'),
    7:  (25000, 1.0, 2.0, 0.5, 'CAUTIOUS'),
    # Score 8-9: medium confidence
    8:  (35000, 1.5, 2.5, 0.6, 'MEDIUM'),
    9:  (45000, 1.5, 2.5, 0.6, 'MEDIUM'),
    # Score 10-12: strong signal
    10: (55000, 2.0, 3.0, 0.7, 'STRONG'),
    11: (60000, 2.0, 3.5, 0.7, 'STRONG'),
    12: (65000, 2.5, 4.0, 0.8, 'STRONG'),
    # Score 13-15: very strong
    13: (70000, 3.0, 4.5, 0.8, 'VERY STRONG'),
    14: (80000, 3.0, 5.0, 0.9, 'VERY STRONG'),
    15: (90000, 3.5, 5.0, 1.0, 'VERY STRONG'),
    # Score 16+: maximum conviction — all in
    16: (100000, 4.0, 6.0, 1.0, 'MAXIMUM'),
}

def get_scale(score: int) -> tuple:
    """Get position size and targets for a given score."""
    score = max(6, min(score, 16))
    # Find closest key
    key = max(k for k in SCALING if k <= score)
    return SCALING[key]

# ── State ─────────────────────────────────────────────────────────────────────
api = None
memory = AgentMemory()
trade_log = []

status = {
    'running':       False,
    'trades_today':  0,
    'active_trade':  None,   # current open trade details
    'last_scan':     None,
    'best_signal':   None,   # best opportunity found in last scan
    'error':         None,
    'daily_pnl':     0.0,
    'mode':          'PAPER',
}


def get_api():
    global api
    if api is None:
        api = tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL, api_version='v2')
    return api


def is_market_open() -> bool:
    try:
        return get_api().get_clock().is_open
    except Exception as e:
        log.warning(f"Clock check failed: {e}")
        return False


def get_bars(symbol: str, timeframe='5Min', limit=60) -> pd.DataFrame:
    for attempt, delay in enumerate([0, 1, 2, 4]):
        try:
            if delay:
                time.sleep(delay)
            now   = datetime.now(pytz.UTC)
            start = now - timedelta(days=5)
            bars  = get_api().get_bars(
                symbol, timeframe,
                start=start.isoformat(),
                end=now.isoformat(),
                limit=limit,
                adjustment='raw',
                feed='iex'
            ).df
            if bars.empty:
                return pd.DataFrame()
            return bars[['open', 'high', 'low', 'close', 'volume']]
        except Exception as e:
            if attempt == 3:
                return pd.DataFrame()
            if '429' in str(e) or '503' in str(e):
                time.sleep(delay)
            else:
                return pd.DataFrame()
    return pd.DataFrame()


def get_positions() -> dict:
    try:
        return {p.symbol: p for p in get_api().list_positions()}
    except:
        return {}


def get_account():
    try:
        return get_api().get_account()
    except:
        return None


def place_order(symbol: str, side: str, qty: int, price: float):
    try:
        get_api().submit_order(
            symbol=symbol, qty=qty, side=side,
            type='market', time_in_force='day'
        )
        log.info(f"ORDER: {side.upper()} {qty}x {symbol} @ ~${price:.2f}")
        entry = {
            'time':   datetime.now().strftime('%H:%M:%S'),
            'date':   datetime.now().strftime('%Y-%m-%d'),
            'symbol': symbol, 'side': side.upper(),
            'qty': qty, 'price': round(price, 2),
        }
        trade_log.append(entry)
        return True
    except Exception as e:
        log.error(f"Order failed {side} {symbol}: {e}")
        status['error'] = str(e)
        return False


def close_position(symbol: str, reason: str = ''):
    positions = get_positions()
    if symbol not in positions:
        return
    p = positions[symbol]
    qty = int(float(p.qty))
    price = float(p.current_price)
    if qty > 0:
        place_order(symbol, 'sell', qty, price)
        log.info(f"Closed {symbol} — {reason}")


def run_cycle():
    """Main trading cycle — scan, find best setup, execute one trade."""
    status['last_scan'] = datetime.now().strftime('%H:%M:%S')

    if not is_market_open():
        log.info("Market closed")
        return

    now_ny = datetime.now(pytz.timezone('America/New_York'))

    # ── Close 15 mins before market close ────────────────────────────────────
    if now_ny.hour == 15 and now_ny.minute >= 45:
        positions = get_positions()
        for sym in positions:
            close_position(sym, 'market closing')
        return

    positions = get_positions()

    # ── Monitor active trade ──────────────────────────────────────────────────
    if status['active_trade'] and positions:
        trade = status['active_trade']
        sym   = trade['symbol']
        if sym in positions:
            p     = positions[sym]
            plpc  = float(p.unrealized_plpc) * 100
            price = float(p.current_price)
            entry = trade['entry_price']
            size  = trade['position_size']
            min_tp = trade['min_target_pct']
            max_tp = trade['max_target_pct']
            sl     = trade['stop_loss_pct']
            score  = trade['score']

            # Dynamic trailing — once we hit min target, trail at 50% of gains
            if plpc >= min_tp:
                # We're in profit — trail aggressively
                trail_stop = plpc * 0.5  # give back max 50% of gains
                if plpc >= max_tp:
                    log.info(f"MAX TARGET HIT: {sym} +{plpc:.2f}% — closing")
                    close_position(sym, f'max target +{plpc:.2f}%')
                    # Record in memory
                    memory.record_trade(
                        sym, trade['pattern'], trade['signal'],
                        entry, price, size, score
                    )
                    status['active_trade'] = None
                    status['trades_today'] += 1
                    status['daily_pnl'] += size * (plpc / 100)
                elif plpc > 0 and plpc < plpc * 0.5:
                    log.info(f"TRAIL STOP: {sym} — locking in gains")
                    close_position(sym, f'trailing stop +{plpc:.2f}%')
                    memory.record_trade(
                        sym, trade['pattern'], trade['signal'],
                        entry, price, size, score
                    )
                    status['active_trade'] = None
                    status['trades_today'] += 1
                    status['daily_pnl'] += size * (plpc / 100)
            elif plpc <= -sl:
                log.warning(f"STOP LOSS: {sym} at {plpc:.2f}%")
                close_position(sym, f'stop loss {plpc:.2f}%')
                memory.record_trade(
                    sym, trade['pattern'], trade['signal'],
                    entry, price, size, score
                )
                status['active_trade'] = None
                status['trades_today'] += 1
                status['daily_pnl'] += size * (plpc / 100)
            else:
                log.info(f"Monitoring {sym}: {plpc:+.2f}% | "
                         f"Target: {min_tp}%-{max_tp}% | Stop: -{sl}%")
        return  # don't scan while in a trade

    # ── Already traded today — done ───────────────────────────────────────────
    if status['trades_today'] >= MAX_DAILY_TRADES:
        log.info(f"Daily trade limit reached ({MAX_DAILY_TRADES}) — done for today")
        return

    # ── Scan for best opportunity ─────────────────────────────────────────────
    log.info(f"Scanning {len(WATCHLIST)} symbols...")
    candidates = scan_market(get_bars)

    if not candidates:
        log.info("No setups found this cycle")
        return

    # Apply memory-based ranking
    ranked = memory.get_best_opportunities(candidates)
    best   = ranked[0]

    status['best_signal'] = best
    log.info(f"Best setup: {best['symbol']} | {best['pattern']} | "
             f"Score: {best['score']} | Signal: {best['signal']} | "
             f"Memory adj: {best.get('memory_adj', 0):+d} | "
             f"Pattern WR: {best.get('pattern_wr', 50)}%")

    # ── Only trade high conviction setups ────────────────────────────────────
    MIN_SCORE = 6
    if best['score'] < MIN_SCORE:
        log.info(f"Best score {best['score']} below minimum {MIN_SCORE} — waiting")
        return

    # ── Get dynamic position size and targets ─────────────────────────────────
    pos_size, min_tp, max_tp, sl_pct, tier = get_scale(best['score'])

    acct = get_account()
    if not acct:
        return
    buying_power = float(acct.buying_power)

    if buying_power < pos_size:
        pos_size = buying_power * 0.9
        log.warning(f"Reduced position to ${pos_size:.0f} (buying power: ${buying_power:.0f})")

    price = best['price']
    qty   = max(1, int(pos_size / price))
    actual_size = qty * price

    log.info(f"ENTERING TRADE: {best['symbol']} | Tier: {tier} | "
             f"Size: ${actual_size:.0f} | Target: {min_tp}%-{max_tp}% | "
             f"Stop: -{sl_pct}% | Expected profit: ${actual_size * min_tp / 100:.0f}-${actual_size * max_tp / 100:.0f}")

    side = 'buy' if best['signal'] == 'BUY' else 'sell'
    success = place_order(best['symbol'], side, qty, price)

    if success:
        status['active_trade'] = {
            'symbol':        best['symbol'],
            'signal':        best['signal'],
            'pattern':       best['pattern'],
            'score':         best['score'],
            'tier':          tier,
            'entry_price':   price,
            'position_size': actual_size,
            'qty':           qty,
            'min_target_pct': min_tp,
            'max_target_pct': max_tp,
            'stop_loss_pct':  sl_pct,
            'entry_time':    datetime.now().strftime('%H:%M:%S'),
        }
        status['error'] = None


def start_bot():
    status['running']      = True
    status['trades_today'] = 0
    log.info("Hardin Trading v2 started — One Big Trade Per Day mode")
    while status['running']:
        try:
            run_cycle()
        except Exception as e:
            log.error(f"Cycle error: {e}")
            status['error'] = str(e)
        time.sleep(60)


def stop_bot():
    status['running'] = False
    log.info("Bot stopped")
