"""
純 pandas 技術指標計算（無外部相依）
取代 pandas-ta，避免 numba 編譯問題
"""

import pandas as pd
import numpy as np


def sma(close, length=5):
    """簡單移動平均"""
    return close.rolling(window=length).mean()


def rsi(close, length=14):
    """相對強弱指標 RSI"""
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=length).mean()
    avg_loss = loss.rolling(window=length).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def macd(close, fast=12, slow=26, signal=9):
    """MACD 指標"""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line

    result = pd.DataFrame({
        "MACD_12_26_9": macd_line,
        "MACDs_12_26_9": signal_line,
        "MACDh_12_26_9": histogram,
    })
    return result


def stoch(high, low, close, k=9, d=3, smooth_k=3):
    """KD 隨機指標"""
    low_min = low.rolling(window=k).min()
    high_max = high.rolling(window=k).max()
    rsv = (close - low_min) / (high_max - low_min).replace(0, np.nan) * 100
    k_line = rsv.ewm(span=smooth_k, adjust=False).mean()
    d_line = k_line.ewm(span=d, adjust=False).mean()

    result = pd.DataFrame({
        f"STOCHk_{k}_{d}_{smooth_k}": k_line,
        f"STOCHd_{k}_{d}_{smooth_k}": d_line,
    })
    return result


def bbands(close, length=20, std=2):
    """布林通道"""
    middle = close.rolling(window=length).mean()
    stddev = close.rolling(window=length).std()
    upper = middle + stddev * std
    lower = middle - stddev * std
    bandwidth = (upper - lower) / middle * 100
    percent_b = (close - lower) / (upper - lower).replace(0, np.nan) * 100

    result = pd.DataFrame({
        f"BBU_{length}_{std}.0": upper,
        f"BBM_{length}_{std}.0": middle,
        f"BBL_{length}_{std}.0": lower,
        f"BBB_{length}_{std}.0": bandwidth,
        f"BBP_{length}_{std}.0": percent_b,
    })
    return result


def adx(high, low, close, length=14):
    """平均趨向指標 ADX"""
    # TR (True Range)
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(window=length).mean()

    # 方向變動
    up_move = high.diff()
    down_move = low.diff()

    plus_dm = pd.Series(0.0, index=high.index)
    minus_dm = pd.Series(0.0, index=high.index)

    plus_dm_mask = (up_move > down_move) & (up_move > 0)
    minus_dm_mask = (down_move > up_move) & (down_move > 0)

    plus_dm[plus_dm_mask] = up_move[plus_dm_mask]
    minus_dm[minus_dm_mask] = down_move[minus_dm_mask]

    # 平滑化
    plus_di = 100 * plus_dm.rolling(window=length).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.rolling(window=length).mean() / atr.replace(0, np.nan)

    # DX / ADX
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_line = dx.rolling(window=length).mean()

    result = pd.DataFrame({
        f"ADX_{length}": adx_line,
        f"DMP_{length}": plus_di,
        f"DMN_{length}": minus_di,
    })
    return result