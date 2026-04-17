"""
Hardin Trading Software v2 — Market Scanner
Continuously scans all stocks for high-conviction setups.
Returns the single best opportunity with a confidence score.
"""
import time
import logging
import pandas as pd
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger('scanner')

# ── Watchlist — high volume liquid stocks ideal for big trades ────────────────
WATCHLIST = [
    # Mega-cap momentum
    'AAPL','MSFT','NVDA','TSLA','META','GOOGL','AMZN','AMD',
    # High volatility growth
    'PLTR','CRWD','COIN','MSTR','SHOP','NET','PANW','SNOW',
    # ETFs — great for gap trades
    'SPY','QQQ','IWM','ARKK','SMH','SOXL','TQQQ',
    # Finance
    'JPM','GS','BAC',
    # Energy
    'XOM','CVX',
    # Biotech — high volatility
    'MRNA','BNTX',
]

# ── Indicators ────────────────────────────────────────────────────────────────

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).iloc[-1]

def atr(bars, period=14):
    """Average True Range — measures volatility"""
    high, low, close = bars['high'], bars['low'], bars['close']
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean().iloc[-1]

def vwap(bars):
    typical = (bars['high'] + bars['low'] + bars['close']) / 3
    return (typical * bars['volume']).cumsum() / bars['volume'].cumsum()

def volume_surge(bars, multiplier=2.0):
    """True if current volume is X times the 20-bar average"""
    avg = bars['volume'].rolling(20).mean().iloc[-1]
    curr = bars['volume'].iloc[-1]
    return curr > avg * multiplier, round(curr / avg, 1)

# ── Pattern Detection ─────────────────────────────────────────────────────────

def detect_breakout(bars):
    """Price breaks above 20-bar high with volume surge"""
    if len(bars) < 22:
        return False, 0
    high_20 = bars['high'].iloc[-21:-1].max()
    curr_close = bars['close'].iloc[-1]
    curr_high = bars['high'].iloc[-1]
    vol_surge, vol_ratio = volume_surge(bars, 1.8)
    if curr_close > high_20 and vol_surge:
        strength = min(10, int(vol_ratio * 2))
        return True, strength
    return False, 0

def detect_breakdown(bars):
    """Price breaks below 20-bar low with volume surge — short opportunity"""
    if len(bars) < 22:
        return False, 0
    low_20 = bars['low'].iloc[-21:-1].min()
    curr_close = bars['close'].iloc[-1]
    vol_surge, vol_ratio = volume_surge(bars, 1.8)
    if curr_close < low_20 and vol_surge:
        strength = min(10, int(vol_ratio * 2))
        return True, strength
    return False, 0

def detect_gap_up(bars):
    """Today's open gaps above yesterday's high"""
    if len(bars) < 2:
        return False, 0
    prev_high = bars['high'].iloc[-2]
    curr_open = bars['open'].iloc[-1]
    curr_close = bars['close'].iloc[-1]
    gap_pct = (curr_open - prev_high) / prev_high * 100
    if gap_pct > 0.5 and curr_close > curr_open:  # gap up + green candle
        strength = min(10, int(gap_pct * 3))
        return True, strength
    return False, 0

def detect_gap_down(bars):
    """Today's open gaps below yesterday's low — short opportunity"""
    if len(bars) < 2:
        return False, 0
    prev_low = bars['low'].iloc[-2]
    curr_open = bars['open'].iloc[-1]
    curr_close = bars['close'].iloc[-1]
    gap_pct = (prev_low - curr_open) / prev_low * 100
    if gap_pct > 0.5 and curr_close < curr_open:  # gap down + red candle
        strength = min(10, int(gap_pct * 3))
        return True, strength
    return False, 0

def detect_bull_reversal(bars):
    """
    Bullish reversal at support:
    - Price near 20-bar low (support)
    - Hammer or bullish engulfing candle
    - RSI oversold
    """
    if len(bars) < 22:
        return False, 0
    score = 0
    price = bars['close'].iloc[-1]
    low_20 = bars['low'].iloc[-20:].min()
    near_support = price < low_20 * 1.02

    # Hammer
    c = bars.iloc[-1]
    body = abs(c['close'] - c['open'])
    lower_wick = min(c['open'], c['close']) - c['low']
    is_hammer = body > 0 and lower_wick >= 2 * body and c['close'] > c['open']

    # Bullish engulfing
    if len(bars) >= 2:
        prev = bars.iloc[-2]
        curr = bars.iloc[-1]
        is_engulfing = (prev['close'] < prev['open'] and
                        curr['close'] > curr['open'] and
                        curr['open'] <= prev['close'] and
                        curr['close'] >= prev['open'])
    else:
        is_engulfing = False

    # RSI oversold
    try:
        r = rsi(bars['close'])
        rsi_oversold = r < 35
    except:
        rsi_oversold = False

    if near_support: score += 3
    if is_hammer: score += 3
    if is_engulfing: score += 3
    if rsi_oversold: score += 2

    return score >= 5, score

def detect_bear_reversal(bars):
    """
    Bearish reversal at resistance:
    - Price near 20-bar high (resistance)
    - Shooting star or bearish engulfing
    - RSI overbought
    """
    if len(bars) < 22:
        return False, 0
    score = 0
    price = bars['close'].iloc[-1]
    high_20 = bars['high'].iloc[-20:].max()
    near_resistance = price > high_20 * 0.98

    # Shooting star
    c = bars.iloc[-1]
    body = abs(c['close'] - c['open'])
    upper_wick = c['high'] - max(c['open'], c['close'])
    is_shooting_star = body > 0 and upper_wick >= 2 * body and c['close'] < c['open']

    # Bearish engulfing
    if len(bars) >= 2:
        prev = bars.iloc[-2]
        curr = bars.iloc[-1]
        is_engulfing = (prev['close'] > prev['open'] and
                        curr['close'] < curr['open'] and
                        curr['open'] >= prev['close'] and
                        curr['close'] <= prev['open'])
    else:
        is_engulfing = False

    try:
        r = rsi(bars['close'])
        rsi_overbought = r > 65
    except:
        rsi_overbought = False

    if near_resistance: score += 3
    if is_shooting_star: score += 3
    if is_engulfing: score += 3
    if rsi_overbought: score += 2

    return score >= 5, score

def detect_momentum_continuation(bars):
    """
    Strong trending move — 3+ consecutive candles in same direction
    with increasing volume. Ride the momentum.
    """
    if len(bars) < 5:
        return False, 0, 'HOLD'
    score = 0
    direction = 'HOLD'

    # Count consecutive green candles
    green_count = 0
    for i in range(-1, -6, -1):
        c = bars.iloc[i]
        if c['close'] > c['open']:
            green_count += 1
        else:
            break

    # Count consecutive red candles
    red_count = 0
    for i in range(-1, -6, -1):
        c = bars.iloc[i]
        if c['close'] < c['open']:
            red_count += 1
        else:
            break

    vol_surge_flag, vol_ratio = volume_surge(bars, 1.5)

    if green_count >= 3:
        score = green_count * 2 + (3 if vol_surge_flag else 0)
        direction = 'BUY'
    elif red_count >= 3:
        score = red_count * 2 + (3 if vol_surge_flag else 0)
        direction = 'SELL'

    return score >= 5, score, direction


# ── Master Signal Scorer ──────────────────────────────────────────────────────

def score_symbol(symbol: str, bars_5m: pd.DataFrame, bars_1m: pd.DataFrame) -> dict | None:
    """
    Score a symbol across all patterns.
    Returns the best setup found with full details.
    """
    if bars_5m.empty or len(bars_5m) < 22:
        return None

    price = float(bars_5m['close'].iloc[-1])
    best_score = 0
    best_signal = 'HOLD'
    best_pattern = 'None'
    patterns_found = []

    # ── Check all patterns ────────────────────────────────────────────────────
    bo, bo_score = detect_breakout(bars_5m)
    if bo:
        patterns_found.append(f'Breakout (score {bo_score})')
        if bo_score > best_score:
            best_score, best_signal, best_pattern = bo_score, 'BUY', 'Breakout'

    bd, bd_score = detect_breakdown(bars_5m)
    if bd:
        patterns_found.append(f'Breakdown SHORT (score {bd_score})')
        if bd_score > best_score:
            best_score, best_signal, best_pattern = bd_score, 'SELL', 'Breakdown Short'

    gu, gu_score = detect_gap_up(bars_5m)
    if gu:
        patterns_found.append(f'Gap Up (score {gu_score})')
        if gu_score > best_score:
            best_score, best_signal, best_pattern = gu_score, 'BUY', 'Gap Up'

    gd, gd_score = detect_gap_down(bars_5m)
    if gd:
        patterns_found.append(f'Gap Down SHORT (score {gd_score})')
        if gd_score > best_score:
            best_score, best_signal, best_pattern = gd_score, 'SELL', 'Gap Down Short'

    br, br_score = detect_bull_reversal(bars_5m)
    if br:
        patterns_found.append(f'Bull Reversal (score {br_score})')
        if br_score > best_score:
            best_score, best_signal, best_pattern = br_score, 'BUY', 'Bull Reversal'

    berr, berr_score = detect_bear_reversal(bars_5m)
    if berr:
        patterns_found.append(f'Bear Reversal (score {berr_score})')
        if berr_score > best_score:
            best_score, best_signal, best_pattern = berr_score, 'SELL', 'Bear Reversal'

    mom, mom_score, mom_dir = detect_momentum_continuation(bars_5m)
    if mom:
        patterns_found.append(f'Momentum {mom_dir} (score {mom_score})')
        if mom_score > best_score:
            best_score, best_signal, best_pattern = mom_score, mom_dir, 'Momentum'

    if best_signal == 'HOLD' or best_score == 0:
        return None

    # ── ATR for dynamic stop/target calculation ───────────────────────────────
    try:
        atr_val = atr(bars_5m)
        atr_pct = (atr_val / price) * 100
    except:
        atr_pct = 0.5

    return {
        'symbol':    symbol,
        'signal':    best_signal,
        'score':     best_score,
        'price':     price,
        'pattern':   best_pattern,
        'patterns':  patterns_found,
        'atr_pct':   round(atr_pct, 3),
    }


def scan_market(get_bars_fn) -> list:
    """
    Scan all symbols in parallel.
    Returns list of opportunities sorted by score (best first).
    """
    results = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {}
        for sym in WATCHLIST:
            futures[pool.submit(_scan_one, sym, get_bars_fn)] = sym
        for future in as_completed(futures):
            result = future.result()
            if result:
                results.append(result)

    return sorted(results, key=lambda x: x['score'], reverse=True)


def _scan_one(symbol, get_bars_fn):
    try:
        bars_5m = get_bars_fn(symbol, '5Min', 60)
        bars_1m = get_bars_fn(symbol, '1Min', 30)
        time.sleep(0.1)
        return score_symbol(symbol, bars_5m, bars_1m)
    except Exception as e:
        log.warning(f"Scan error {symbol}: {e}")
        return None
