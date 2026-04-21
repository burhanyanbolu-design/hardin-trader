"""
Hardin Trading Software v2 — Flask Dashboard
"""
import os
import threading
from flask import Flask, jsonify, render_template, request

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


# ── Entry point ───────────────────────────────────────────────────────────────
@app.route('/health')
def health():
    return jsonify({'status': 'ok'}), 200


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False)

