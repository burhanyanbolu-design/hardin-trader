"""
Hardin Trading Software v2 — Agent Memory System
Each agent learns from every pattern it sees and tracks outcomes.
Memory persists to disk so agents get smarter every day.
"""
import json
import os
import logging
from datetime import datetime
from collections import defaultdict

log = logging.getLogger('memory')

MEMORY_FILE = 'agent_memory.json'


class AgentMemory:
    """
    Persistent memory for a trading agent.
    Tracks pattern outcomes per symbol and learns win rates over time.
    """

    def __init__(self):
        self.memory = self._load()

    def _load(self) -> dict:
        if os.path.exists(MEMORY_FILE):
            try:
                with open(MEMORY_FILE, 'r') as f:
                    return json.load(f)
            except:
                pass
        return {
            'patterns': {},      # pattern_key -> {wins, losses, total, avg_pct}
            'symbols':  {},      # symbol -> {wins, losses, best_pattern, avg_pct}
            'trades':   [],      # full trade history
            'last_updated': None
        }

    def _save(self):
        try:
            self.memory['last_updated'] = datetime.now().isoformat()
            with open(MEMORY_FILE, 'w') as f:
                json.dump(self.memory, f, indent=2)
        except Exception as e:
            log.warning(f"Memory save failed: {e}")

    def record_trade(self, symbol: str, pattern: str, signal: str,
                     entry_price: float, exit_price: float,
                     position_size: float, score: int):
        """Record a completed trade and update pattern/symbol memory."""
        pct = ((exit_price - entry_price) / entry_price) * 100
        if signal == 'SELL':  # short trade — profit when price goes down
            pct = -pct
        profit = position_size * (pct / 100)
        won = pct > 0

        trade = {
            'date':     datetime.now().strftime('%Y-%m-%d'),
            'time':     datetime.now().strftime('%H:%M:%S'),
            'symbol':   symbol,
            'pattern':  pattern,
            'signal':   signal,
            'entry':    round(entry_price, 2),
            'exit':     round(exit_price, 2),
            'pct':      round(pct, 3),
            'profit':   round(profit, 2),
            'size':     round(position_size, 2),
            'score':    score,
            'won':      won,
        }
        self.memory['trades'].append(trade)

        # Update pattern memory
        pk = f"{pattern}_{signal}"
        if pk not in self.memory['patterns']:
            self.memory['patterns'][pk] = {
                'wins': 0, 'losses': 0, 'total': 0,
                'total_pct': 0, 'avg_pct': 0, 'win_rate': 0
            }
        p = self.memory['patterns'][pk]
        p['total'] += 1
        p['total_pct'] += pct
        p['avg_pct'] = round(p['total_pct'] / p['total'], 3)
        if won:
            p['wins'] += 1
        else:
            p['losses'] += 1
        p['win_rate'] = round(p['wins'] / p['total'] * 100, 1)

        # Update symbol memory
        if symbol not in self.memory['symbols']:
            self.memory['symbols'][symbol] = {
                'wins': 0, 'losses': 0, 'total': 0,
                'total_pct': 0, 'avg_pct': 0, 'win_rate': 0,
                'best_pattern': None, 'best_pattern_wr': 0
            }
        s = self.memory['symbols'][symbol]
        s['total'] += 1
        s['total_pct'] += pct
        s['avg_pct'] = round(s['total_pct'] / s['total'], 3)
        if won:
            s['wins'] += 1
        else:
            s['losses'] += 1
        s['win_rate'] = round(s['wins'] / s['total'] * 100, 1)

        # Track best pattern for this symbol
        if p['win_rate'] > s['best_pattern_wr'] and p['total'] >= 3:
            s['best_pattern'] = pk
            s['best_pattern_wr'] = p['win_rate']

        self._save()
        log.info(f"Memory updated: {symbol} {pattern} {signal} {pct:+.2f}% | "
                 f"Pattern win rate: {p['win_rate']}% ({p['total']} trades)")
        return trade

    def get_pattern_confidence(self, pattern: str, signal: str) -> dict:
        """
        Get historical win rate for a pattern.
        Returns confidence boost/penalty for signal scoring.
        """
        pk = f"{pattern}_{signal}"
        if pk not in self.memory['patterns']:
            return {'win_rate': 50, 'total': 0, 'avg_pct': 0, 'confidence_adj': 0}

        p = self.memory['patterns'][pk]
        # Confidence adjustment: +3 if >70% win rate, -3 if <40%
        if p['total'] < 5:
            adj = 0  # not enough data yet
        elif p['win_rate'] >= 70:
            adj = 3
        elif p['win_rate'] >= 60:
            adj = 2
        elif p['win_rate'] >= 50:
            adj = 1
        elif p['win_rate'] < 40:
            adj = -3
        else:
            adj = -1

        return {
            'win_rate':       p['win_rate'],
            'total':          p['total'],
            'avg_pct':        p['avg_pct'],
            'confidence_adj': adj,
        }

    def get_symbol_confidence(self, symbol: str) -> dict:
        """Get historical performance for a specific symbol."""
        if symbol not in self.memory['symbols']:
            return {'win_rate': 50, 'total': 0, 'best_pattern': None}
        return self.memory['symbols'][symbol]

    def get_best_opportunities(self, candidates: list) -> list:
        """
        Re-rank scan candidates using memory.
        Boosts score for patterns/symbols with proven track records.
        Penalises patterns that historically lose.
        """
        enhanced = []
        for c in candidates:
            sym = c['symbol']
            pat = c['pattern']
            sig = c['signal']

            pat_conf = self.get_pattern_confidence(pat, sig)
            sym_conf = self.get_symbol_confidence(sym)

            # Apply memory-based score adjustment
            memory_adj = pat_conf['confidence_adj']
            if sym_conf['total'] >= 5:
                if sym_conf['win_rate'] >= 65:
                    memory_adj += 2
                elif sym_conf['win_rate'] < 40:
                    memory_adj -= 2

            enhanced_score = c['score'] + memory_adj
            enhanced.append({
                **c,
                'score':          enhanced_score,
                'raw_score':      c['score'],
                'memory_adj':     memory_adj,
                'pattern_wr':     pat_conf['win_rate'],
                'pattern_trades': pat_conf['total'],
                'symbol_wr':      sym_conf['win_rate'],
                'symbol_trades':  sym_conf['total'],
            })

        return sorted(enhanced, key=lambda x: x['score'], reverse=True)

    def get_stats(self) -> dict:
        """Summary stats for dashboard."""
        trades = self.memory['trades']
        if not trades:
            return {'total': 0, 'wins': 0, 'losses': 0, 'win_rate': 0,
                    'total_profit': 0, 'avg_profit': 0, 'best_pattern': None}

        wins = [t for t in trades if t['won']]
        total_profit = sum(t['profit'] for t in trades)
        best_pattern = max(
            self.memory['patterns'].items(),
            key=lambda x: x[1]['win_rate'] if x[1]['total'] >= 3 else 0,
            default=(None, {})
        )

        return {
            'total':        len(trades),
            'wins':         len(wins),
            'losses':       len(trades) - len(wins),
            'win_rate':     round(len(wins) / len(trades) * 100, 1),
            'total_profit': round(total_profit, 2),
            'avg_profit':   round(total_profit / len(trades), 2),
            'best_pattern': best_pattern[0],
            'best_wr':      best_pattern[1].get('win_rate', 0) if best_pattern[1] else 0,
        }
