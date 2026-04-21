"""
Hardin Trading Software v2 — One Big Trade Per Day
Dynamic position sizing and profit targets based on signal strength.
Agents learn from every trade and get smarter over time.
"""
import os, time, logging
from datetime import datetime, timedelta
import pytz
import pandas as pd
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from scanner import scan_market, WATCHLIST
from agent_memory import AgentMemory

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('trader')

# ── Config ────────────────────────────────────────────────────────────────────
API_KEY    = os.getenv('ALPACA_API_KEY')
SECRET_KEY = os.getenv('ALPACA_SECRET_KEY')
PAPER      = os.getenv('ALPACA_BASE_URL', 'https://paper-api.alpaca.markets').startswith('https://paper')

MAX_DAILY_TRADES    = 10
DAILY_PROFIT_TARGET = float(os.getenv('DAILY_PROFIT_TARGET', 1000))
OVERRIDE_SCORE      = 14

# ── Dynamic scaling table ─────────────────────────────────────────────────────
SCALING = {
    6:  (20000, 1.0, 1.5, 0.5, 'CAUTIOUS'),
    7:  (25000, 1.0, 2.0, 0.5, 'CAUTIOUS'),
    8:  (35000, 1.5, 2.5, 0.6, 'MEDIUM'),
    9:  (45000, 1.5, 2.5, 0.6, 'MEDIUM'),
    10: (55000, 2.0, 3.0, 0.7, 'STRONG'),
    11: (60000, 2.0, 3.5, 0.7, 'STRONG'),
    12: (65000, 2.5, 4.0, 0.8, 'STRONG'),
    13: (70000, 3.0, 4.5, 0.8, 'VERY STRONG'),
    14: (80000, 3.0, 5.0, 0.9, 'VERY STRONG'),
    15: (90000, 3.5, 5.0, 1.0, 'VERY STRONG'),
    16: (100000, 4.0, 6.0, 1.0, 'MAXIMUM'),
}

def get_scale(score: int) -> tuple:
    score = max(6, min(score, 16))
    key = max(k for k in SCALING if k <= score)
    return SCALING[key]

# ── Clients ───────────────────────────────────────────────────────────────────
_trading_client = None
_data_client    = None
memory          = AgentMemory()
trade_log       = []

status = {
    'running':      False,
    'trades_today': 0,
    'active_trade': None,
    'last_scan':    None,
    'best_signal':  None,
    'error':        None,
    'daily_pnl':    0.0,
    'target_hit':   False,
    'mode':         'PAPER' if PAPER else 'LIVE',
}


def get_trading_client() -> TradingClient:
    global _trading_client
    if _trading_client is None:
        _trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
    return _trading_client


def get_data_client() -> StockHistoricalDataClient:
    global _data_client
    if _data_client is None:
        _data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
    return _data_client


def is_market_open() -> bool:
    try:
        clock = get_trading_client().get_clock()
        return clock.is_open
    except Exception as e:
        log.warning(f"Clock check failed: {e}")
        return False


def get_bars(symbol: str, timeframe='5Min', limit=60) -> pd.DataFrame:
    """Fetch OHLCV bars using alpaca-py."""
    tf_map = {
        '1Min':  TimeFrame(1,  TimeFrameUnit.Minute),
        '5Min':  TimeFrame(5,  TimeFrameUnit.Minute),
        '15Min': TimeFrame(15, TimeFrameUnit.Minute),
        '1Hour': TimeFrame(1,  TimeFrameUnit.Hour),
        '1Day':  TimeFrame(1,  TimeFrameUnit.Day),
    }
    tf = tf_map.get(timeframe, TimeFrame(5, TimeFrameUnit.Minute))

    for attempt, delay in enumerate([0, 1, 2, 4]):
        try:
            if delay:
                time.sleep(delay)
            now   = datetime.now(pytz.UTC)
            start = now - timedelta(days=5)
            req = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=tf,
                start=start,
                end=now,
                limit=limit,
                feed='iex',
            )
            bars = get_data_client().get_stock_bars(req).df
            if bars.empty:
                return pd.DataFrame()
            # alpaca-py returns multi-index (symbol, timestamp) — drop symbol level
            if isinstance(bars.index, pd.MultiIndex):
                bars = bars.droplevel(0)
            bars.index.name = 'timestamp'
            return bars[['open', 'high', 'low', 'close', 'volume']]
        except Exception as e:
            if attempt == 3:
                log.warning(f"get_bars failed for {symbol}: {e}")
                return pd.DataFrame()
            if '429' in str(e) or '503' in str(e):
                time.sleep(delay + 1)
    return pd.DataFrame()


def get_positions() -> dict:
    try:
        positions = get_trading_client().get_all_positions()
        return {p.symbol: p for p in positions}
    except Exception as e:
        log.warning(f"get_positions failed: {e}")
        return {}


def get_account():
    try:
        return get_trading_client().get_account()
    except Exception as e:
        log.warning(f"get_account failed: {e}")
        return None


def place_order(symbol: str, side: str, qty: int, price: float) -> bool:
    try:
        order_side = OrderSide.BUY if side == 'buy' else OrderSide.SELL
        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=order_side,
            time_in_force=TimeInForce.DAY,
        )
        get_trading_client().submit_order(req)
        log.info(f"ORDER: {side.upper()} {qty}x {symbol} @ ~${price:.2f}")
        trade_log.append({
            'time':   datetime.now().strftime('%H:%M:%S'),
            'date':   datetime.now().strftime('%Y-%m-%d'),
            'symbol': symbol,
            'side':   side.upper(),
            'qty':    qty,
            'price':  round(price, 2),
        })
        return True
    except Exception as e:
        log.error(f"Order failed {side} {symbol}: {e}")
        status['error'] = str(e)
        return False


def close_position(symbol: str, reason: str = ''):
    positions = get_positions()
    if symbol not in positions:
        return
    p   = positions[symbol]
    qty = int(float(p.qty))
    price = float(p.current_price)
    if qty > 0:
        place_order(symbol, 'sell', qty, price)
        log.info(f"Closed {symbol} — {reason}")


def run_cycle():
    status['last_scan'] = datetime.now().strftime('%H:%M:%S')

    if not is_market_open():
        log.info("Market closed")
        return

    now_ny = datetime.now(pytz.timezone('America/New_York'))

    # Close 15 mins before market close
    if now_ny.hour == 15 and now_ny.minute >= 45:
        for sym in get_positions():
            close_position(sym, 'market closing')
        return

    positions = get_positions()

    # Monitor active trade
    if status['active_trade'] and positions:
        trade = status['active_trade']
        sym   = trade['symbol']
        if sym in positions:
            p      = positions[sym]
            plpc   = float(p.unrealized_plpc) * 100
            price  = float(p.current_price)
            entry  = trade['entry_price']
            size   = trade['position_size']
            min_tp = trade['min_target_pct']
            max_tp = trade['max_target_pct']
            sl     = trade['stop_loss_pct']
            score  = trade['score']

            if plpc >= max_tp:
                log.info(f"MAX TARGET HIT: {sym} +{plpc:.2f}% — closing")
                close_position(sym, f'max target +{plpc:.2f}%')
                memory.record_trade(sym, trade['pattern'], trade['signal'], entry, price, size, score)
                status['active_trade'] = None
                status['trades_today'] += 1
                status['daily_pnl']    += size * (plpc / 100)
            elif plpc >= min_tp and plpc < plpc * 0.5:
                log.info(f"TRAIL STOP: {sym} — locking in gains")
                close_position(sym, f'trailing stop +{plpc:.2f}%')
                memory.record_trade(sym, trade['pattern'], trade['signal'], entry, price, size, score)
                status['active_trade'] = None
                status['trades_today'] += 1
                status['daily_pnl']    += size * (plpc / 100)
            elif plpc <= -sl:
                log.warning(f"STOP LOSS: {sym} at {plpc:.2f}%")
                close_position(sym, f'stop loss {plpc:.2f}%')
                memory.record_trade(sym, trade['pattern'], trade['signal'], entry, price, size, score)
                status['active_trade'] = None
                status['trades_today'] += 1
                status['daily_pnl']    += size * (plpc / 100)
            else:
                log.info(f"Monitoring {sym}: {plpc:+.2f}% | Target: {min_tp}%-{max_tp}% | Stop: -{sl}%")
        return

    # Check daily target
    if status['daily_pnl'] >= DAILY_PROFIT_TARGET:
        log.info(f"Daily target hit (${status['daily_pnl']:.2f}) — only trading score {OVERRIDE_SCORE}+ setups")
        status['target_hit'] = True
    else:
        status['target_hit'] = False

    # Scan for best opportunity
    log.info(f"Scanning {len(WATCHLIST)} symbols...")
    candidates = scan_market(get_bars)

    if not candidates:
        log.info("No setups found this cycle")
        return

    ranked = memory.get_best_opportunities(candidates)
    best   = ranked[0]
    status['best_signal'] = best

    log.info(f"Best setup: {best['symbol']} | {best['pattern']} | "
             f"Score: {best['score']} | Signal: {best['signal']}")

    MIN_SCORE = 6
    if status.get('target_hit') and best['score'] < OVERRIDE_SCORE:
        log.info(f"Target hit — skipping score {best['score']} (need {OVERRIDE_SCORE}+)")
        return

    if best['score'] < MIN_SCORE:
        log.info(f"Best score {best['score']} below minimum {MIN_SCORE} — waiting")
        return

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

    log.info(f"ENTERING: {best['symbol']} | Tier: {tier} | Size: ${actual_size:.0f} | "
             f"Target: {min_tp}%-{max_tp}% | Stop: -{sl_pct}%")

    side    = 'buy' if best['signal'] == 'BUY' else 'sell'
    success = place_order(best['symbol'], side, qty, price)

    if success:
        status['active_trade'] = {
            'symbol':         best['symbol'],
            'signal':         best['signal'],
            'pattern':        best['pattern'],
            'score':          best['score'],
            'tier':           tier,
            'entry_price':    price,
            'position_size':  actual_size,
            'qty':            qty,
            'min_target_pct': min_tp,
            'max_target_pct': max_tp,
            'stop_loss_pct':  sl_pct,
            'entry_time':     datetime.now().strftime('%H:%M:%S'),
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
