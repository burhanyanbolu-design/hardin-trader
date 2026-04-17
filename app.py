"""
Hardin Trading Software v2 — Web Dashboard
"""
import threading
from flask import Flask, jsonify, render_template_string
from flask_cors import CORS
import trader
from agent_memory import AgentMemory

app = Flask(__name__)
CORS(app)
memory = AgentMemory()
bot_thread = None

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hardin Trading v2</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family: 'Segoe UI', sans-serif; background:#050508; color:#f2f0ea; }
nav { background:#0a0a14; border-bottom:1px solid rgba(0,229,255,0.15); padding:16px 32px; display:flex; justify-content:space-between; align-items:center; }
.logo { font-size:18px; font-weight:900; letter-spacing:3px; color:#00e5ff; }
.logo span { color:#fff; }
.status-dot { width:10px; height:10px; border-radius:50%; display:inline-block; margin-right:6px; }
.dot-live { background:#00e5ff; animation:pulse 1.5s infinite; }
.dot-off  { background:#555; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
.main { padding:24px 32px; display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:16px; }
.card { background:#0f0f1a; border:1px solid rgba(0,229,255,0.12); border-radius:8px; padding:20px; }
.card-title { font-size:10px; letter-spacing:2px; text-transform:uppercase; color:#00e5ff; margin-bottom:12px; }
.big-num { font-size:2.4rem; font-weight:900; color:#00e5ff; }
.big-num.green { color:#2ecc71; }
.big-num.red   { color:#e74c3c; }
.big-num.gold  { color:#f0c040; }
.label { font-size:11px; color:#555; margin-top:4px; }
.trade-box { background:#0a0a14; border:1px solid rgba(240,192,64,0.3); border-radius:6px; padding:14px; margin-top:8px; }
.trade-row { display:flex; justify-content:space-between; font-size:12px; padding:4px 0; border-bottom:1px solid rgba(255,255,255,0.05); }
.trade-row:last-child { border:none; }
.tier-badge { display:inline-block; padding:3px 10px; border-radius:3px; font-size:10px; font-weight:700; letter-spacing:1px; }
.tier-MAX      { background:#c0392b; color:#fff; }
.tier-VERY     { background:#e67e22; color:#fff; }
.tier-STRONG   { background:#f0c040; color:#000; }
.tier-MEDIUM   { background:#3498db; color:#fff; }
.tier-CAUTIOUS { background:#555; color:#fff; }
.btn { padding:10px 24px; border:none; border-radius:4px; font-size:12px; font-weight:700; letter-spacing:1px; cursor:pointer; text-transform:uppercase; transition:all 0.2s; }
.btn-start { background:#00e5ff; color:#000; }
.btn-stop  { background:#e74c3c; color:#fff; }
.btn:hover { opacity:0.85; transform:translateY(-1px); }
.memory-grid { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-top:8px; }
.mem-item { background:#0a0a14; border-radius:4px; padding:8px 10px; font-size:11px; }
.mem-label { color:#555; font-size:10px; }
.mem-val { color:#f2f0ea; font-weight:600; margin-top:2px; }
.scan-list { max-height:200px; overflow-y:auto; margin-top:8px; }
.scan-item { display:flex; justify-content:space-between; align-items:center; padding:6px 0; border-bottom:1px solid rgba(255,255,255,0.04); font-size:12px; }
.score-bar { height:4px; background:rgba(0,229,255,0.15); border-radius:2px; margin-top:4px; }
.score-fill { height:4px; background:#00e5ff; border-radius:2px; transition:width 0.3s; }
</style>
</head>
<body>
<nav>
  <div class="logo">HARDIN <span>TRADING</span> v2</div>
  <div style="display:flex;gap:12px;align-items:center;">
    <span id="statusText" style="font-size:12px;color:#555;">OFFLINE</span>
    <button class="btn btn-start" onclick="startBot()">START</button>
    <button class="btn btn-stop"  onclick="stopBot()">STOP</button>
  </div>
</nav>

<div class="main">

  <!-- Daily P&L -->
  <div class="card">
    <div class="card-title">Today's P&L</div>
    <div class="big-num green" id="dailyPnl">$0.00</div>
    <div class="label">Daily profit / loss</div>
    <div id="targetStatus" style="font-size:11px;color:#555;margin-top:6px;">Hunting for setups...</div>
    <div style="margin-top:12px;display:flex;gap:16px;">
      <div><div style="font-size:18px;font-weight:700;" id="tradesCount">0</div><div class="label">Trades today</div></div>
      <div><div style="font-size:18px;font-weight:700;color:#2ecc71;" id="winRate">-</div><div class="label">All-time win rate</div></div>
    </div>
  </div>

  <!-- Active Trade -->
  <div class="card">
    <div class="card-title">Active Trade</div>
    <div id="activeTrade">
      <div style="color:#555;font-size:13px;">No active trade — scanning for setup</div>
    </div>
  </div>

  <!-- Best Signal -->
  <div class="card">
    <div class="card-title">Best Signal Found</div>
    <div id="bestSignal">
      <div style="color:#555;font-size:13px;">Scanning...</div>
    </div>
  </div>

  <!-- Memory Stats -->
  <div class="card">
    <div class="card-title">Agent Memory</div>
    <div id="memoryStats">
      <div style="color:#555;font-size:13px;">No trades recorded yet</div>
    </div>
  </div>

</div>

<script>
async function fetchStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();

    // Status
    document.getElementById('statusText').textContent = d.running ? '● LIVE' : '○ OFFLINE';
    document.getElementById('statusText').style.color = d.running ? '#00e5ff' : '#555';

    // P&L
    const pnl = d.daily_pnl || 0;
    const pnlEl = document.getElementById('dailyPnl');
    pnlEl.textContent = (pnl >= 0 ? '+' : '') + '$' + Math.abs(pnl).toFixed(2);
    pnlEl.className = 'big-num ' + (pnl >= 0 ? 'green' : 'red');

    document.getElementById('tradesCount').textContent = d.trades_today || 0;
    // Show target hit warning
    const targetEl = document.getElementById('targetStatus');
    if (targetEl) {
      if (d.target_hit) {
        targetEl.textContent = '✓ Daily target hit — only trading score 14+ overrides';
        targetEl.style.color = '#f0c040';
      } else {
        targetEl.textContent = 'Hunting for setups...';
        targetEl.style.color = '#555';
      }
    }

    // Memory stats
    const mem = d.memory || {};
    if (mem.total > 0) {
      document.getElementById('winRate').textContent = mem.win_rate + '%';
      document.getElementById('memoryStats').innerHTML = `
        <div class="memory-grid">
          <div class="mem-item"><div class="mem-label">Total Trades</div><div class="mem-val">${mem.total}</div></div>
          <div class="mem-item"><div class="mem-label">Win Rate</div><div class="mem-val" style="color:#2ecc71">${mem.win_rate}%</div></div>
          <div class="mem-item"><div class="mem-label">Total Profit</div><div class="mem-val" style="color:${mem.total_profit>=0?'#2ecc71':'#e74c3c'}">${mem.total_profit>=0?'+':''}$${Math.abs(mem.total_profit).toFixed(2)}</div></div>
          <div class="mem-item"><div class="mem-label">Avg Per Trade</div><div class="mem-val">${mem.avg_profit>=0?'+':''}$${Math.abs(mem.avg_profit||0).toFixed(2)}</div></div>
        </div>
        ${mem.best_pattern ? `<div style="margin-top:8px;font-size:11px;color:#f0c040;">Best pattern: ${mem.best_pattern} (${mem.best_wr}% WR)</div>` : ''}
      `;
    }

    // Active trade
    const at = d.active_trade;
    if (at) {
      document.getElementById('activeTrade').innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
          <span style="font-size:20px;font-weight:900;color:#f0c040;">${at.symbol}</span>
          <span class="tier-badge tier-${at.tier?.split(' ')[0]}">${at.tier}</span>
        </div>
        <div class="trade-box">
          <div class="trade-row"><span>Pattern</span><span style="color:#00e5ff">${at.pattern}</span></div>
          <div class="trade-row"><span>Signal</span><span style="color:${at.signal==='BUY'?'#2ecc71':'#e74c3c'}">${at.signal}</span></div>
          <div class="trade-row"><span>Entry</span><span>$${at.entry_price?.toFixed(2)}</span></div>
          <div class="trade-row"><span>Size</span><span>$${Math.round(at.position_size||0).toLocaleString()}</span></div>
          <div class="trade-row"><span>Target</span><span style="color:#2ecc71">${at.min_target_pct}% → ${at.max_target_pct}%</span></div>
          <div class="trade-row"><span>Stop</span><span style="color:#e74c3c">-${at.stop_loss_pct}%</span></div>
          <div class="trade-row"><span>Score</span><span style="color:#f0c040">${at.score}</span></div>
        </div>
      `;
    } else {
      document.getElementById('activeTrade').innerHTML = '<div style="color:#555;font-size:13px;">No active trade — scanning for setup</div>';
    }

    // Best signal
    const bs = d.best_signal;
    if (bs) {
      document.getElementById('bestSignal').innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
          <span style="font-size:18px;font-weight:900;color:#fff;">${bs.symbol}</span>
          <span style="color:${bs.signal==='BUY'?'#2ecc71':'#e74c3c'};font-weight:700;">${bs.signal}</span>
        </div>
        <div class="score-bar"><div class="score-fill" style="width:${Math.min(100,bs.score*6)}%"></div></div>
        <div style="margin-top:8px;font-size:12px;color:#555;">
          Score: <span style="color:#f0c040;font-weight:700;">${bs.score}</span> |
          Pattern: <span style="color:#00e5ff">${bs.pattern}</span> |
          $${bs.price?.toFixed(2)}
        </div>
        ${bs.pattern_wr ? `<div style="font-size:11px;color:#888;margin-top:4px;">Pattern WR: ${bs.pattern_wr}% (${bs.pattern_trades} trades)</div>` : ''}
      `;
    }

  } catch(e) { console.error(e); }
}

async function startBot() {
  await fetch('/api/start', {method:'POST'});
  fetchStatus();
}
async function stopBot() {
  await fetch('/api/stop', {method:'POST'});
  fetchStatus();
}

fetchStatus();
setInterval(fetchStatus, 5000);
</script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(DASHBOARD_HTML)

@app.route('/api/status')
def api_status():
    mem_stats = memory.get_stats()
    return jsonify({
        **trader.status,
        'memory':      mem_stats,
        'trade_log':   trader.trade_log[-20:],
    })

@app.route('/api/start', methods=['POST'])
def api_start():
    global bot_thread
    if not trader.status['running']:
        trader.status['trades_today'] = 0
        trader.status['daily_pnl']    = 0.0
        bot_thread = threading.Thread(target=trader.start_bot, daemon=True)
        bot_thread.start()
    return jsonify({'ok': True})

@app.route('/api/stop', methods=['POST'])
def api_stop():
    trader.stop_bot()
    return jsonify({'ok': True})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=False)
