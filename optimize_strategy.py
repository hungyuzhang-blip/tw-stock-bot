"""測試多種策略變體，找出較高勝率組合"""
import yfinance as yf
import pandas as pd
import pandas_ta as ta
import twstock

FORECAST_DAYS = 10
BACKTEST_PERIOD = "5y"

HOT_STOCK_POOL = {
    "AI與半導體龍頭": ["2330", "2317", "3711"],
    "AI伺服器與ODM": ["3231", "6669", "3706"],
    "散熱與伺服器零組件": ["3017", "3324", "2059", "8210"],
    "漲價概念-矽晶圓與記憶體": ["6488", "3532", "2408", "2344"],
    "漲價概念-被動元件與PCB": ["2327", "2383", "6274", "8046"],
}


def get_symbol(stock_id):
    info = twstock.codes.get(stock_id)
    return f"{stock_id}.TWO" if info and info.market == "上櫃" else f"{stock_id}.TW"


def prepare_df(df):
    kd = ta.stoch(high=df["High"], low=df["Low"], close=df["Close"], k=9, d=3, smooth_k=3)
    df = pd.concat([df, kd], axis=1)
    df["RSI"] = ta.rsi(close=df["Close"], length=14)
    macd = ta.macd(close=df["Close"], fast=12, slow=26, signal=9)
    df = pd.concat([df, macd], axis=1)
    df["SMA20"] = ta.sma(close=df["Close"], length=20)
    df["SMA60"] = ta.sma(close=df["Close"], length=60)
    bb = ta.bbands(close=df["Close"], length=20, std=2)
    df = pd.concat([df, bb], axis=1)
    adx = ta.adx(df["High"], df["Low"], df["Close"], length=14)
    df = pd.concat([df, adx], axis=1)
    df["VOL_MA5"] = ta.sma(close=df["Volume"], length=5)
    df["VOL_MA20"] = ta.sma(close=df["Volume"], length=20)
    return df


def row_vals(df, i):
    kc = next(c for c in df.columns if c.startswith("STOCHk_"))
    dc = next(c for c in df.columns if c.startswith("STOCHd_"))
    bbl = next(c for c in df.columns if c.startswith("BBL_"))
    bbm = next(c for c in df.columns if c.startswith("BBM_"))
    row, prev = df.iloc[i], df.iloc[i - 1]
    vol = int(row["Volume"]) or int(prev["Volume"])
    return row, prev, kc, dc, bbl, bbm, vol


def is_strong_bear(row):
    return (
        row["ADX_14"] >= 25
        and row["Close"] < row["SMA20"] < row["SMA60"]
    )


def test_variant(name, fn):
    stats = {"n": 0, "win": 0, "rets": []}
    for stock_id in [x for v in HOT_STOCK_POOL.values() for x in v]:
        try:
            df = yf.Ticker(get_symbol(stock_id)).history(period=BACKTEST_PERIOD)
        except Exception:
            continue
        if len(df) < 80:
            continue
        df = prepare_df(df)
        for i in range(60, len(df) - FORECAST_DAYS):
            row, prev, kc, dc, bbl, bbm, vol = row_vals(df, i)
            if pd.isna(row["SMA60"]) or pd.isna(row["RSI"]):
                continue
            if not fn(row, prev, kc, dc, bbl, bbm, vol):
                continue
            past, future = row["Close"], df.iloc[i + FORECAST_DAYS]["Close"]
            if past <= 0:
                continue
            ret = (future - past) / past
            stats["n"] += 1
            stats["win"] += int(ret > 0)
            stats["rets"].append(ret * 100)
    if stats["n"]:
        wr = stats["win"] / stats["n"] * 100
        avg = sum(stats["rets"]) / len(stats["rets"])
        print(f"{name:28} | 觸發 {stats['n']:4} | 勝率 {wr:5.1f}% | 報酬 {avg:+6.2f}%")
    else:
        print(f"{name:28} | 觸發    0 | 勝率   N/A")


def main():
    variants = []

    # A: 多頭趨勢回檔買進
    variants.append(("A 多頭回檔", lambda r, p, kc, dc, bbl, bbm, vol: (
        r["Close"] > r["SMA60"] and r["SMA20"] > r["SMA60"]
        and 35 <= r["RSI"] <= 52
        and r["MACDh_12_26_9"] > p["MACDh_12_26_9"]
        and r[kc] > r[dc]
        and r["VOL_MA20"] > 0 and vol > r["VOL_MA20"] * 0.8
    )))

    # B: 超賣反彈 + 趨勢過濾（非強勢空頭）
    variants.append(("B 超賣反彈過濾", lambda r, p, kc, dc, bbl, bbm, vol: (
        r["RSI"] < 35
        and (r["Close"] <= r[bbl] * 1.03 or r[bbl] < r["Close"] < r[bbm])
        and r[kc] > r[dc] and (p[kc] <= p[dc] or r[kc] < 35)
        and r["VOL_MA5"] > 0 and vol > r["VOL_MA5"] * 1.2
        and not is_strong_bear(r)
    )))

    # C: 動能突破
    variants.append(("C 動能突破", lambda r, p, kc, dc, bbl, bbm, vol: (
        r["Close"] > r["SMA20"] > r["SMA60"]
        and r["MACD_12_26_9"] > r["MACDs_12_26_9"]
        and p["MACD_12_26_9"] <= p["MACDs_12_26_9"]
        and 50 <= r["RSI"] <= 68
        and r["VOL_MA20"] > 0 and vol > r["VOL_MA20"] * 1.3
    )))

    # D: B + 站穩季線附近
    variants.append(("D 超賣+季線支撐", lambda r, p, kc, dc, bbl, bbm, vol: (
        r["RSI"] < 38
        and r["Close"] >= r["SMA60"] * 0.95
        and r[kc] > r[dc] and p[kc] <= p[dc]
        and r["VOL_MA5"] > 0 and vol > r["VOL_MA5"] * 1.1
        and not is_strong_bear(r)
    )))

    # E: A or B (分層)
    variants.append(("E A或B", lambda r, p, kc, dc, bbl, bbm, vol: (
        variants[0][1](r, p, kc, dc, bbl, bbm, vol) or variants[1][1](r, p, kc, dc, bbl, bbm, vol)
    )))

    # F: B with stricter volume
    variants.append(("F 超賣+放量1.5x", lambda r, p, kc, dc, bbl, bbm, vol: (
        r["RSI"] < 35
        and (r["Close"] <= r[bbl] * 1.03 or r[bbl] < r["Close"] < r[bbm])
        and r[kc] > r[dc] and p[kc] <= p[dc]
        and r["VOL_MA5"] > 0 and vol > r["VOL_MA5"] * 1.5
        and not is_strong_bear(r)
    )))

    # G: 原策略2+3改良
    variants.append(("G 改良2+3", lambda r, p, kc, dc, bbl, bbm, vol: (
        r["RSI"] < 40
        and (r["Close"] <= r[bbl] * 1.02 or r[bbl] < r["Close"] < r[bbm])
        and r[kc] > r[dc] and p[kc] <= p[dc] and r[kc] < 40
        and r["VOL_MA5"] > 0 and vol > r["VOL_MA5"] * 1.2
        and not is_strong_bear(r)
    )))

    a_fn, c_fn, g_fn = variants[0][1], variants[2][1], variants[6][1]
    variants.append(("H 多頭回檔或突破", lambda r, p, kc, dc, bbl, bbm, vol: (
        a_fn(r, p, kc, dc, bbl, bbm, vol) or c_fn(r, p, kc, dc, bbl, bbm, vol)
    )))
    variants.append(("I 突破或超賣改良", lambda r, p, kc, dc, bbl, bbm, vol: (
        c_fn(r, p, kc, dc, bbl, bbm, vol) or g_fn(r, p, kc, dc, bbl, bbm, vol)
    )))
    variants.append(("J 三場景任一", lambda r, p, kc, dc, bbl, bbm, vol: (
        a_fn(r, p, kc, dc, bbl, bbm, vol) or c_fn(r, p, kc, dc, bbl, bbm, vol) or g_fn(r, p, kc, dc, bbl, bbm, vol)
    )))

    print("=" * 70)
    print("策略變體回測（主流股池 5 年）")
    print("=" * 70)
    for name, fn in variants:
        test_variant(name, fn)


if __name__ == "__main__":
    main()
