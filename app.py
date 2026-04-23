"""
Hardin Trading Software v2 — Flask Dashboard
"""
import os
import threading
from flask import Flask, jsonify, render_template, request
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

import trader
from agent_memory import AgentMemory

app = Flask(__name__)
memory = trader.memory

# ── Bot thread management ─────────────────────────────────────────────────────
_bot_thread = None

def _bot_loop():
    trader.start_bot()

def _start_bot_thread():
    global _bot_thread
    if _bot_thread is None or not _bot_thread.is_alive():
        trader.status['running']      = False
        trader.status['trades_today'] = 0
        trader.status['daily_pnl']    = 0.0
        trader.status['target_hit']   = False
        trader.status['error']        = None
        _bot_thread = threading.Thread(target=_bot_loop, daemon=True)
        _bot_thread.start()

def _stop_bot():
    trader.stop_bot()

# ── Auto scheduler — 9:25am start, 4:00pm stop (New York time) ───────────────
_scheduler = BackgroundScheduler(timezone=pytz.timezone('America/New_York'))
_scheduler.add_job(_start_bot_thread, 'cron', day_of_week='mon-fri', hour=9,  minute=25)
_scheduler.add_job(_stop_bot,         'cron', day_of_week='mon-fri', hour=16, minute=0)
_scheduler.start()

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/status')
def api_status():
    acct = trader.get_account()
    equity     = float(acct.equity)     if acct else 0.0
    cash       = float(acct.cash)       if acct else 0.0
    last_equity = float(acct.last_equity) if acct else equity
    daily_pnl  = equity - last_equity

    # Enrich active_trade with live P&L %
    active = trader.status.get('active_trade')
    if active:
        positions = trader.get_positions()
        sym = active.get('symbol')
        if sym and sym in positions:
            pos = positions[sym]
            # unrealized_plpc is already a decimal in alpaca-py (e.g. 0.015 = 1.5%)
            raw_plpc = float(pos.unrealized_plpc)
            live_plpc = raw_plpc * 100 if abs(raw_plpc) < 1 else raw_plpc
            active = {**active, 'live_plpc': round(live_plpc, 3)}
        else:
            active = {**active, 'live_plpc': 0.0}

    mem_stats = memory.get_stats()

    return jsonify({
        **trader.status,
        'active_trade': active,
        'equity':       round(equity, 2),
        'cash':         round(cash, 2),
        'daily_pnl':    round(daily_pnl, 2),
        'memory':       mem_stats,
        'trade_log':    trader.trade_log[-20:],
        'auto_schedule': 'Mon-Fri: Auto-start 9:25am NY · Auto-stop 4:00pm NY',
    })


@app.route('/api/start', methods=['POST'])
def api_start():
    _start_bot_thread()
    return jsonify({'ok': True, 'running': trader.status['running']})


@app.route('/api/stop', methods=['POST'])
def api_stop():
    _stop_bot()
    return jsonify({'ok': True, 'running': False})


@app.route('/api/override', methods=['POST'])
def api_override():
    trader.status['target_hit']   = False
    trader.status['daily_pnl']    = 0.0
    trader.status['trades_today'] = 0
    trader.status['error']        = None
    _start_bot_thread()
    return jsonify({'ok': True, 'status': trader.status})


@app.route('/api/bars/<symbol>')
def api_bars(symbol):
    df = trader.get_bars(symbol.upper(), '5Min', 60)
    if df.empty:
        return jsonify([])
    df = df.reset_index()
    # index column may be 'timestamp' or 'time'
    time_col = 'timestamp' if 'timestamp' in df.columns else df.columns[0]
    rows = []
    for _, row in df.iterrows():
        rows.append({
            'time':  str(row[time_col]),
            'open':  round(float(row['open']),  4),
            'high':  round(float(row['high']),  4),
            'low':   round(float(row['low']),   4),
            'close': round(float(row['close']), 4),
        })
    return jsonify(rows)


@app.route('/api/signals')
def api_signals():
    try:
        from scanner import scan_market
        results = scan_market(trader.get_bars)
        return jsonify(results[:15])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/backtest', methods=['POST'])
def api_backtest():
    """Run backtest using yfinance historical data."""
    try:
        import yfinance as yf
        from datetime import datetime, timedelta
        from scanner import score_symbol, WATCHLIST

        days     = int(request.json.get('days', 90)) if request.json else 90
        symbols  = request.json.get('symbols', WATCHLIST) if request.json else WATCHLIST
        capital  = 100_000.0
        results  = []
        trades   = []
        equity   = capital
        peak     = capital
        max_dd   = 0.0

        end   = datetime.utcnow()
        start = end - timedelta(days=days)

        for sym in symbols:
            try:
                df = yf.download(sym, start=start, end=end,
                                 interval='1d', progress=False, auto_adjust=True)
                if df.empty or len(df) < 30:
                    continue
                df.columns = [c.lower() for c in df.columns]
                df = df[['open','high','low','close','volume']].dropna()

                # Replay signals day by day
                for i in range(26, len(df)):
                    window = df.iloc[:i+1]
                    sig    = score_symbol(sym, window, window)
                    if not sig or sig['signal'] == 'HOLD' or sig['score'] < 6:
                        continue

                    from trader import get_scale
                    pos_size, min_tp, max_tp, sl_pct, tier = get_scale(sig['score'])
                    pos_size = min(pos_size, equity * 0.9)
                    if pos_size < 1000:
                        continue

                    entry_price = float(df['close'].iloc[i])
                    qty         = max(1, int(pos_size / entry_price))
                    cost        = qty * entry_price

                    # Simulate next-day exit
                    if i + 1 >= len(df):
                        continue
                    next_bar   = df.iloc[i + 1]
                    exit_high  = float(next_bar['high'])
                    exit_low   = float(next_bar['low'])
                    exit_close = float(next_bar['close'])

                    # Check take-profit and stop-loss on next bar
                    tp_price = entry_price * (1 + max_tp / 100)
                    sl_price = entry_price * (1 - sl_pct / 100)

                    if sig['signal'] == 'BUY':
                        if exit_low <= sl_price:
                            exit_price = sl_price
                        elif exit_high >= tp_price:
                            exit_price = tp_price
                        else:
                            exit_price = exit_close
                    else:  # SELL/SHORT
                        if exit_high >= entry_price * (1 + sl_pct / 100):
                            exit_price = entry_price * (1 + sl_pct / 100)
                        elif exit_low <= entry_price * (1 - max_tp / 100):
                            exit_price = entry_price * (1 - max_tp / 100)
                        else:
                            exit_price = exit_close

                    pnl = (exit_price - entry_price) * qty
                    if sig['signal'] == 'SELL':
                        pnl = -pnl

                    equity += pnl
                    if equity > peak:
                        peak = equity
                    dd = (peak - equity) / peak * 100
                    if dd > max_dd:
                        max_dd = dd

                    trades.append({
                        'symbol':      sym,
                        'signal':      sig['signal'],
                        'pattern':     sig['pattern'],
                        'score':       sig['score'],
                        'tier':        tier,
                        'entry_date':  str(df.index[i])[:10],
                        'exit_date':   str(df.index[i+1])[:10],
                        'entry_price': round(entry_price, 2),
                        'exit_price':  round(exit_price, 2),
                        'qty':         qty,
                        'pnl':         round(pnl, 2),
                        'win':         pnl > 0,
                    })

            except Exception as e:
                log.warning(f"Backtest error {sym}: {e}")
                continue

        total_pnl  = equity - capital
        wins       = [t for t in trades if t['win']]
        losses     = [t for t in trades if not t['win']]
        win_rate   = len(wins) / len(trades) * 100 if trades else 0
        avg_win    = sum(t['pnl'] for t in wins)    / len(wins)   if wins   else 0
        avg_loss   = sum(t['pnl'] for t in losses)  / len(losses) if losses else 0
        pf         = abs(avg_win * len(wins) / (avg_loss * len(losses))) if losses and avg_loss != 0 else 0

        return jsonify({
            'initial_capital': capital,
            'final_capital':   round(equity, 2),
            'total_pnl':       round(total_pnl, 2),
            'total_pnl_pct':   round(total_pnl / capital * 100, 2),
            'total_trades':    len(trades),
            'wins':            len(wins),
            'losses':          len(losses),
            'win_rate':        round(win_rate, 1),
            'avg_win':         round(avg_win, 2),
            'avg_loss':        round(avg_loss, 2),
            'profit_factor':   round(pf, 2),
            'max_drawdown':    round(max_dd, 2),
            'trades':          trades[-50:],  # last 50 trades
        })

    except Exception as e:
        log.error(f"Backtest failed: {e}")
        return jsonify({'error': str(e)}), 500


# ── Entry point ───────────────────────────────────────────────────────────────
@app.route('/health')
def health():
    return jsonify({'status': 'ok'}), 200


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False)

