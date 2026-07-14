"""回測目前三重策略邏輯的歷史勝率"""
import yfinance as yf
import pandas as pd
import pandas_ta as ta
import twstock

FORECAST_DAYS = 10
BACKTEST_PERIOD = "5y"
BBL_NEAR_RATIO = 1.02
STRATEGY_VOL_RATIO = 1.2
RECOMMEND_MIN_WIN_RATE = 58
STRONG_RECOMMEND_MIN_WIN_RATE = 55

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


def compute_indicators(df):
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


def evaluate_strategies(df, idx):
    k_col = next(c for c in df.columns if c.startswith("STOCHk_"))
    d_col = next(c for c in df.columns if c.startswith("STOCHd_"))
    bbl_col = next(c for c in df.columns if c.startswith("BBL_"))
    bbm_col = next(c for c in df.columns if c.startswith("BBM_"))

    row = df.iloc[idx]
    prev = df.iloc[idx - 1]
    if pd.isna(row["SMA60"]) or pd.isna(row["RSI"]) or pd.isna(row[k_col]):
        return None

    close = row["Close"]
    volume = int(row["Volume"])
    if volume <= 0:
        for offset in range(1, 4):
            volume = int(df.iloc[idx - offset]["Volume"])
            if volume > 0:
                break

    scenario_a = (
        close > row["SMA60"]
        and row["SMA20"] > row["SMA60"]
        and 35 <= row["RSI"] <= 52
        and row["MACDh_12_26_9"] > prev["MACDh_12_26_9"]
        and row[k_col] > row[d_col]
        and row["VOL_MA20"] > 0
        and volume > row["VOL_MA20"] * 0.8
    )
    scenario_b = (
        close > row["SMA20"] > row["SMA60"]
        and row["MACD_12_26_9"] > row["MACDs_12_26_9"]
        and prev["MACD_12_26_9"] <= prev["MACDs_12_26_9"]
        and 50 <= row["RSI"] <= 68
        and row["VOL_MA20"] > 0
        and volume > row["VOL_MA20"] * 1.3
    )
    near_lower = close <= row[bbl_col] * BBL_NEAR_RATIO
    between_bands = row[bbl_col] < close < row[bbm_col]
    strong_bear = row["ADX_14"] >= 25 and close < row["SMA20"] < row["SMA60"]
    scenario_c = (
        row["RSI"] < 40
        and (near_lower or between_bands)
        and row[k_col] > row[d_col]
        and prev[k_col] <= prev[d_col]
        and row[k_col] < 40
        and row["VOL_MA5"] > 0
        and volume > row["VOL_MA5"] * STRATEGY_VOL_RATIO
        and not strong_bear
    )
    return scenario_a, scenario_b, scenario_c


def estimate_similar_up_prob(df, idx):
    k_col = next(c for c in df.columns if c.startswith("STOCHk_"))
    row = df.iloc[idx]
    cur_k, cur_rsi = row[k_col], row["RSI"]
    up_count = total = 0

    for i in range(60, len(df) - FORECAST_DAYS):
        if i == idx:
            continue
        sample = df.iloc[i]
        if pd.isna(sample[k_col]) or pd.isna(sample["RSI"]):
            continue
        if abs(sample[k_col] - cur_k) > 15 or abs(sample["RSI"] - cur_rsi) > 10:
            continue
        past = sample["Close"]
        future = df.iloc[i + FORECAST_DAYS]["Close"]
        if past <= 0:
            continue
        total += 1
        if future > past:
            up_count += 1

    if total < 5:
        return None
    return round(up_count / total * 100, 1)


def run_backtest():
    stats = {
        "strategy_1": {"n": 0, "win": 0, "rets": []},
        "strategy_2": {"n": 0, "win": 0, "rets": []},
        "strategy_3": {"n": 0, "win": 0, "rets": []},
        "dual_scenario": {"n": 0, "win": 0, "rets": []},
        "full_recommend": {"n": 0, "win": 0, "rets": []},
        "today_pool_latest": {"n": 0, "win": 0},
    }
    per_stock = {}

    stock_ids = [sid for ids in HOT_STOCK_POOL.values() for sid in ids]
    for stock_id in stock_ids:
        try:
            df = yf.Ticker(get_symbol(stock_id)).history(period=BACKTEST_PERIOD)
        except Exception:
            continue
        if len(df) < 80:
            continue

        df = compute_indicators(df)
        triple_n = triple_win = 0

        for i in range(60, len(df) - FORECAST_DAYS):
            result = evaluate_strategies(df, i)
            if not result:
                continue

            s1, s2, s3 = result
            past = df.iloc[i]["Close"]
            future = df.iloc[i + FORECAST_DAYS]["Close"]
            if past <= 0:
                continue

            ret = (future - past) / past
            up = ret > 0

            for key, flag in [("strategy_1", s1), ("strategy_2", s2), ("strategy_3", s3)]:
                if flag:
                    stats[key]["n"] += 1
                    stats[key]["win"] += int(up)
                    stats[key]["rets"].append(ret * 100)

            pass_count = sum([s1, s2, s3])
            primary = s2 or s3

            if pass_count >= 2:
                stats["dual_scenario"]["n"] += 1
                stats["dual_scenario"]["win"] += int(up)
                stats["dual_scenario"]["rets"].append(ret * 100)
                triple_n += 1
                triple_win += int(up)

            up_prob = None
            if pass_count >= 1 or primary:
                up_prob = estimate_similar_up_prob(df, i)
            if up_prob is not None:
                strong = pass_count >= 2 and up_prob > STRONG_RECOMMEND_MIN_WIN_RATE
                normal = primary and up_prob > RECOMMEND_MIN_WIN_RATE
                if strong or normal:
                    stats["full_recommend"]["n"] += 1
                    stats["full_recommend"]["win"] += int(up)
                    stats["full_recommend"]["rets"].append(ret * 100)

        latest = evaluate_strategies(df, len(df) - 1)
        if latest:
            s1, s2, s3 = latest
            stats["today_pool_latest"]["n"] += 1
            stats["today_pool_latest"]["win"] += int(sum([s1, s2, s3]) >= 2)

        if triple_n:
            per_stock[stock_id] = {
                "n": triple_n,
                "win_rate": round(triple_win / triple_n * 100, 1),
            }

    return stats, per_stock


def print_report(stats, per_stock):
    labels = {
        "strategy_1": "策略一（均線+MACD）單獨",
        "strategy_2": "策略二（布林+RSI）單獨",
        "strategy_3": "策略三（KD+成交量）單獨",
        "dual_scenario": "雙場景以上共振",
        "full_recommend": "完整推薦條件（優化策略 + 回測勝率門檻）",
        "today_pool_latest": "今日主流池最新一天三重共振觸發檔數",
    }

    print("=" * 50)
    print("主流股池 5 年歷史回測（未來 2 週 = 10 交易日）")
    print("=" * 50)
    for key, label in labels.items():
        data = stats[key]
        if key == "today_pool_latest":
            print(f"\n{label}：{data['win']} / {data['n']} 檔")
            continue
        if data["n"] == 0:
            print(f"\n{label}")
            print(f"  觸發次數：0（樣本不足，無法評估）")
            continue
        win_rate = data["win"] / data["n"] * 100
        avg_ret = sum(data["rets"]) / len(data["rets"])
        print(f"\n{label}")
        print(f"  觸發次數：{data['n']}")
        print(f"  實際勝率：{win_rate:.1f}%")
        print(f"  平均 2 週報酬：{avg_ret:+.2f}%")

    print("\n" + "-" * 50)
    print("各股「三重共振」歷史觸發統計")
    print("-" * 50)
    if not per_stock:
        print("  無任何股票在 5 年內出現三重共振")
    for stock_id, info in sorted(per_stock.items(), key=lambda x: -x[1]["n"]):
        name = twstock.codes[stock_id].name if stock_id in twstock.codes else "未知"
        print(f"  {stock_id} {name}：{info['n']} 次｜勝率 {info['win_rate']}%")


def run_pair_backtest():
    combos = {
        "1+2": lambda s1, s2, s3: s1 and s2,
        "1+3": lambda s1, s2, s3: s1 and s3,
        "2+3": lambda s1, s2, s3: s2 and s3,
    }
    stats = {key: {"n": 0, "win": 0, "rets": []} for key in combos}
    per_stock = {key: {} for key in combos}

    stock_ids = [sid for ids in HOT_STOCK_POOL.values() for sid in ids]
    for stock_id in stock_ids:
        try:
            df = yf.Ticker(get_symbol(stock_id)).history(period=BACKTEST_PERIOD)
        except Exception:
            continue
        if len(df) < 80:
            continue

        df = compute_indicators(df)
        stock_stats = {key: {"n": 0, "win": 0} for key in combos}

        for i in range(60, len(df) - FORECAST_DAYS):
            result = evaluate_strategies(df, i)
            if not result:
                continue

            s1, s2, s3 = result
            past = df.iloc[i]["Close"]
            future = df.iloc[i + FORECAST_DAYS]["Close"]
            if past <= 0:
                continue

            ret = (future - past) / past
            up = ret > 0

            for name, check in combos.items():
                if check(s1, s2, s3):
                    stats[name]["n"] += 1
                    stats[name]["win"] += int(up)
                    stats[name]["rets"].append(ret * 100)
                    stock_stats[name]["n"] += 1
                    stock_stats[name]["win"] += int(up)

        for name in combos:
            if stock_stats[name]["n"]:
                per_stock[name][stock_id] = round(
                    stock_stats[name]["win"] / stock_stats[name]["n"] * 100, 1
                )

    return stats, per_stock


def print_pair_report(stats, per_stock):
    labels = {
        "1+2": "策略一 + 策略二（趨勢動能 + 超賣反彈）",
        "1+3": "策略一 + 策略三（趨勢動能 + KD放量轉折）",
        "2+3": "策略二 + 策略三（超賣反彈 + KD放量轉折）",
    }
    print("=" * 50)
    print("策略組合 5 年回測（未來 2 週 = 10 交易日）")
    print("=" * 50)
    for key, label in labels.items():
        data = stats[key]
        print(f"\n{label}")
        if data["n"] == 0:
            print("  觸發次數：0（樣本不足）")
            continue
        win_rate = data["win"] / data["n"] * 100
        avg_ret = sum(data["rets"]) / len(data["rets"])
        print(f"  觸發次數：{data['n']}")
        print(f"  實際勝率：{win_rate:.1f}%")
        print(f"  平均 2 週報酬：{avg_ret:+.2f}%")
        if per_stock[key]:
            top = sorted(per_stock[key].items(), key=lambda x: -x[1])[:3]
            names = ", ".join(f"{sid}({wr}%)" for sid, wr in top)
            print(f"  個股最高勝率：{names}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "pairs":
        pair_stats, pair_per_stock = run_pair_backtest()
        print_pair_report(pair_stats, pair_per_stock)
    else:
        stats, per_stock = run_backtest()
        print_report(stats, per_stock)

