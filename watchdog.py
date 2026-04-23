"""
Hardin Trading Watchdog
Monitors the Hardin Trading v2 app running on Railway.
Watches for: errors, P&L thresholds, stale activity, wash trades.
Runs locally or as a separate Railway service.
"""
import os
import time
import logging
import requests
from datetime import datetime
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient

load_dotenv()
log = logging.getLogger('watchdog')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# ── Config ────────────────────────────────────────────────────────────────────
APP_URL             = os.getenv('WATCHDOG_APP_URL', 'https://mellow-cooperation-production-f0d1.up.railway.app')
API_KEY             = os.getenv('ALPACA_API_KEY')
SECRET_KEY          = os.getenv('ALPACA_SECRET_KEY')
PAPER               = os.getenv('ALPACA_BASE_URL', 'https://paper-api.alpaca.markets').startswith('https://paper')

MAX_DAILY_LOSS      = float(os.getenv('MAX_DAILY_LOSS', -500))
CHECK_INTERVAL      = int(os.getenv('CHECK_INTERVAL_SECS', 60))
STALE_THRESHOLD     = int(os.getenv('STALE_THRESHOLD_MINS', 10))

# ── Clients ───────────────────────────────────────────────────────────────────
_trading_client = None

def get_trading_client():
    global _trading_client
    if _trading_client is None:
        _trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
    return _trading_client


# ── App Health Check ──────────────────────────────────────────────────────────
def check_app_health() -> dict | None:
    """Ping the Railway app health and status endpoints."""
    try:
        r = requests.get(f'{APP_URL}/health', timeout=10)
        if r.status_code != 200:
            log.error(f'❌ Health check failed: HTTP {r.status_code}')
            return None
        log.info(f'✅ App is reachable')
    except Exception as e:
        log.error(f'❌ App unreachable: {e}')
        return None

    try:
        r = requests.get(f'{APP_URL}/api/status', timeout=10)
        return r.json()
    except Exception as e:
        log.error(f'❌ Status fetch failed: {e}')
        return None


# ── P&L Monitor ───────────────────────────────────────────────────────────────
_alert_triggered = False

def check_pnl():
    global _alert_triggered
    try:
        acct   = get_trading_client().get_account()
        equity = float(acct.equity)
        pnl    = equity - float(acct.last_equity)
        bp     = float(acct.buying_power)
        icon   = '📈' if pnl >= 0 else '📉'
        log.info(f'{icon} Equity: ${equity:,.2f} | Daily P&L: ${pnl:+,.2f} | Buying Power: ${bp:,.2f}')

        if pnl <= MAX_DAILY_LOSS and not _alert_triggered:
            log.error(f'🚨 MAX LOSS ALERT! Daily P&L ${pnl:+,.2f} hit threshold ${MAX_DAILY_LOSS:,.2f}')
            _alert_triggered = True

        if pnl > MAX_DAILY_LOSS * 0.8:
            _alert_triggered = False

        return pnl
    except Exception as e:
        log.error(f'❌ P&L check failed: {e}')
        return None


# ── Order Monitor ─────────────────────────────────────────────────────────────
def check_failed_orders():
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        req    = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=20)
        orders = get_trading_client().get_orders(req)
        for o in orders:
            if o.status.value in ('rejected', 'canceled', 'expired'):
                log.warning(f'⚠️  Order issue: {o.symbol} {o.side.value} {o.qty} — {o.status.value}')
    except Exception as e:
        log.error(f'❌ Order check failed: {e}')


# ── Active Trade Monitor ──────────────────────────────────────────────────────
_last_active_trade = None
_last_activity_time = datetime.now()

def check_active_trade(status: dict):
    global _last_active_trade, _last_activity_time

    active = status.get('active_trade')
    running = status.get('running', False)

    if not running:
        log.info('ℹ️  Bot is not running (outside market hours or stopped)')
        return

    if active:
        sym   = active.get('symbol', '?')
        plpc  = active.get('live_plpc', 0)
        score = active.get('score', 0)
        tier  = active.get('tier', '?')
        entry = active.get('entry_price', 0)
        icon  = '📈' if plpc >= 0 else '📉'
        log.info(f'{icon} Active: {sym} | Tier: {tier} | Score: {score} | Entry: ${entry:.2f} | P&L: {plpc:+.2f}%')
        _last_activity_time = datetime.now()
        _last_active_trade  = sym
    else:
        # Check for stale inactivity during market hours
        silence_mins = (datetime.now() - _last_activity_time).total_seconds() / 60
        if silence_mins > STALE_THRESHOLD:
            log.warning(f'⚠️  No active trade for {silence_mins:.0f} mins — bot may be stuck')

    # Log any errors from the app
    error = status.get('error')
    if error:
        log.warning(f'⚠️  App error: {error}')


# ── Summary ───────────────────────────────────────────────────────────────────
def print_summary(status: dict, pnl: float | None):
    trades_today = status.get('trades_today', 0)
    target_hit   = status.get('target_hit', False)
    memory       = status.get('memory', {})
    win_rate     = memory.get('win_rate', 0)
    total_trades = memory.get('total', 0)

    log.info('─' * 50)
    log.info(f'  Trades today : {trades_today}')
    log.info(f'  Target hit   : {"✅ YES" if target_hit else "❌ No"}')
    log.info(f'  Memory       : {total_trades} total trades | {win_rate}% win rate')
    if pnl is not None:
        log.info(f'  Daily P&L    : ${pnl:+,.2f}')
    log.info('─' * 50)


# ── Main Loop ─────────────────────────────────────────────────────────────────
def main():
    log.info('=' * 55)
    log.info('  🐕 HARDIN TRADING WATCHDOG STARTED')
    log.info(f'  Monitoring: {APP_URL}')
    log.info(f'  Check interval: {CHECK_INTERVAL}s')
    log.info(f'  Max daily loss: ${MAX_DAILY_LOSS:,.2f}')
    log.info('=' * 55)

    check_count = 0
    while True:
        check_count += 1
        log.info(f'── Check #{check_count} @ {datetime.now().strftime("%H:%M:%S")} ──')

        # 1. Check app is alive
        status = check_app_health()
        if status is None:
            log.error('🚨 App is DOWN or unreachable!')
            time.sleep(CHECK_INTERVAL)
            continue

        # 2. Check active trade and bot status
        check_active_trade(status)

        # 3. Check P&L via Alpaca
        pnl = check_pnl()

        # 4. Check for failed orders
        check_failed_orders()

        # 5. Print summary
        print_summary(status, pnl)

        time.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    main()
