import yfinance as yf
import pandas as pd
import numpy as np
import ta_lib
import twstock
import requests
import urllib3
import re
import os
import csv
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

FORECAST_DAYS = 10
BACKTEST_PERIOD = "2y"
ADX_TREND_THRESHOLD = 25
VOLUME_CONFIRM_RATIO = 0.8
VOLUME_LOW_RATIO = 0.6
VOLUME_STRONG_RATIO = 1.2
RECOMMEND_MIN_WIN_RATE = 50
STRONG_RECOMMEND_MIN_WIN_RATE = 50
TRENDING_TOP_N = 5
STOCK_CODE_PATTERN = re.compile(r"(?<![0-9])([1-9][0-9]{3})(?![0-9])")
YAHOO_QUOTE_PATTERN = re.compile(r"/quote/(\d{4})")
TWSE_HEADERS = {"User-Agent": "Mozilla/5.0"}
SCAN_RESULTS_DIR = "scan_results"
FULL_SCAN_WORKERS = 5
FULL_SCAN_BATCH_SIZE = 30
SCAN_FAST_PERIOD = "2y"

HOT_STOCK_POOL = {
    "AI與半導體龍頭": ["2330", "2317", "3711"],
    "AI伺服器與ODM": ["3231", "6669", "3706"],
    "散熱與伺服器零組件": ["3017", "3324", "2059", "8210"],
    "漲價概念-矽晶圓與記憶體": ["6488", "3532", "2408", "2344"],
    "漲價概念-被動元件與PCB": ["2327", "2383", "6274", "8046"],
}

K_COL = "STOCHk_9_3_3"
D_COL = "STOCHd_9_3_3"
BBL_NEAR_RATIO = 1.02
STRATEGY_VOL_RATIO = 1.2

# ─── 新策略評分權重 ────────────────────────────────────
SCORE_THRESHOLD_BUY = 6        # 總分 >= 6 → 買入訊號
SCORE_THRESHOLD_STRONG = 10    # 總分 >= 10 → 強烈買入
SCORE_THRESHOLD_WATCH = 4      # 總分 >= 4 → 觀察
RECOMMEND_MIN_WIN_RATE = 50
STRONG_RECOMMEND_MIN_WIN_RATE = 50

def get_stock_name(stock_id):
    info = twstock.codes.get(stock_id)
    return info.name if info else "未知"

def get_stock_market(stock_id):
    info = twstock.codes.get(stock_id)
    return info.market if info else "上市"

def get_yfinance_symbol(stock_id):
    market = get_stock_market(stock_id)
    suffix = "TWO" if market == "上櫃" else "TW"
    return f"{stock_id}.{suffix}"

def get_hot_pool_stock_ids():
    stock_ids = set()
    for ids in HOT_STOCK_POOL.values():
        stock_ids.update(ids)
    return stock_ids

def get_all_tw_stock_ids():
    stock_ids = []
    for code, info in twstock.codes.items():
        if info.type != "股票":
            continue
        if info.market not in ("上市", "上櫃"):
            continue
        stock_ids.append(code)
    return sorted(stock_ids)

def is_valid_stock_code(stock_id):
    return stock_id in twstock.codes

def extract_stock_codes_from_text(text):
    if not text:
        return []
    return [code for code in STOCK_CODE_PATTERN.findall(text) if is_valid_stock_code(code)]

def get_web_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-TW,zh;q=0.9",
    })
    session.cookies.set("over18", "1")
    return session

def fetch_ptt_stock_titles(session, max_pages=10):
    titles = []
    for page_idx in range(max_pages):
        page = "index.html" if page_idx == 0 else f"index{page_idx}.html"
        url = f"https://www.ptt.cc/bbs/Stock/{page}"
        try:
            resp = session.get(url, timeout=20)
            if resp.status_code != 200:
                break
            soup = BeautifulSoup(resp.text, "html.parser")
            page_titles = [
                anchor.get_text(strip=True)
                for anchor in soup.select("div.r-ent div.title a")
            ]
            if not page_titles:
                break
            titles.extend(page_titles)
        except requests.RequestException:
            break
    return titles

def fetch_yahoo_finance_mentions(session):
    texts = []
    urls = [
        "https://tw.stock.yahoo.com/news/",
        "https://tw.stock.yahoo.com/rank/volume",
        "https://tw.stock.yahoo.com/tw-market",
    ]
    for url in urls:
        try:
            resp = session.get(url, timeout=20)
            if resp.status_code != 200:
                continue
            texts.append(resp.text)
            soup = BeautifulSoup(resp.text, "html.parser")
            for anchor in soup.find_all("a", href=True):
                href = anchor.get("href", "")
                texts.append(anchor.get_text(" ", strip=True))
                texts.append(href)
            for tag in soup.find_all(["h1", "h2", "h3", "p", "span"]):
                texts.append(tag.get_text(" ", strip=True))
        except requests.RequestException:
            continue

    codes = []
    for text in texts:
        codes.extend(YAHOO_QUOTE_PATTERN.findall(text))
        codes.extend(extract_stock_codes_from_text(text))
    return list(dict.fromkeys(codes)), texts

def fetch_trending_stocks():
    session = get_web_session()
    pool_ids = get_hot_pool_stock_ids()
    mention_counter = Counter()
    source_details = {"ptt_titles": [], "yahoo_codes": [], "errors": []}

    try:
        ptt_titles = fetch_ptt_stock_titles(session)
        source_details["ptt_titles"] = ptt_titles
        for title in ptt_titles:
            mention_counter.update(extract_stock_codes_from_text(title))
    except Exception as exc:
        source_details["errors"].append(f"PTT 股版擷取失敗：{exc}")

    try:
        yahoo_codes, _ = fetch_yahoo_finance_mentions(session)
        source_details["yahoo_codes"] = yahoo_codes
        mention_counter.update(yahoo_codes)
    except Exception as exc:
        source_details["errors"].append(f"Yahoo 財經擷取失敗：{exc}")

    top_5_mentioned = mention_counter.most_common(TRENDING_TOP_N)
    new_trending_stocks = []
    for stock_id, _ in mention_counter.most_common(50):
        if stock_id not in pool_ids:
            new_trending_stocks.append(stock_id)
        if len(new_trending_stocks) >= TRENDING_TOP_N:
            break

    return {
        "top_5_mentioned": top_5_mentioned,
        "new_trending_stocks": new_trending_stocks,
        "mention_counter": mention_counter,
        "source_details": source_details,
    }

def parse_twse_number(value):
    return int(str(value).replace(",", ""))

def fetch_recent_trading_dates(max_days=20):
    dates = []
    day = datetime.now()
    for _ in range(max_days):
        day -= timedelta(days=1)
        dates.append(day.strftime("%Y%m%d"))
    return dates

def fetch_institutional_data(stock_id, market, lookback=5):
    records = []
    for date_str in fetch_recent_trading_dates(30):
        if market == "上市":
            url = (
                "https://www.twse.com.tw/fund/T86"
                f"?response=json&date={date_str}&selectType=ALLBUT0999"
            )
            try:
                resp = requests.get(url, headers=TWSE_HEADERS, timeout=15)
                data = resp.json()
            except (requests.RequestException, ValueError):
                continue
            if data.get("stat") != "OK":
                continue
            for row in data.get("data", []):
                if row[0].strip() != stock_id:
                    continue
                records.append({
                    "date": date_str,
                    "foreign": parse_twse_number(row[4]),
                    "trust": parse_twse_number(row[11]),
                    "dealer": parse_twse_number(row[12]),
                    "total": parse_twse_number(row[18]),
                })
                break
        else:
            y, m, d = date_str[:4], date_str[4:6], date_str[6:8]
            url = (
                "https://www.tpex.org.tw/web/stock/3insti/daily_trade/"
                f"3itrade_hedge_result.php?l=zh-tw&se=EW&t=D&d={y}/{m}/{d}&s=0,asc,0,0"
            )
            try:
                resp = requests.get(url, headers=TWSE_HEADERS, timeout=15, verify=False)
                data = resp.json()
            except (requests.RequestException, ValueError):
                continue
            for row in data.get("aaData", []):
                if row[0].strip() != stock_id:
                    continue
                records.append({
                    "date": date_str,
                    "foreign": parse_twse_number(row[4]),
                    "trust": parse_twse_number(row[7]),
                    "dealer": parse_twse_number(row[10]),
                    "total": parse_twse_number(row[13]),
                })
                break
        if len(records) >= lookback:
            break
    return records

def fetch_margin_data(stock_id):
    for date_str in fetch_recent_trading_dates(30):
        url = (
            "https://www.twse.com.tw/exchangeReport/MI_MARGN"
            f"?response=json&date={date_str}&selectType=ALL"
        )
        try:
            resp = requests.get(url, headers=TWSE_HEADERS, timeout=15)
            data = resp.json()
        except (requests.RequestException, ValueError):
            continue
        if data.get("stat") != "OK" or len(data.get("tables", [])) < 2:
            continue
        for row in data["tables"][1].get("data", []):
            if str(row[0]).strip() != stock_id:
                continue
            prev_margin = parse_twse_number(row[5])
            curr_margin = parse_twse_number(row[6])
            prev_short = parse_twse_number(row[11])
            curr_short = parse_twse_number(row[12])
            return {
                "date": date_str,
                "margin_change": curr_margin - prev_margin,
                "short_change": curr_short - prev_short,
                "margin_balance": curr_margin,
                "short_balance": curr_short,
            }
    return None

def shares_to_lots(shares):
    return round(shares / 1000, 1)

def consecutive_direction(values):
    if not values:
        return 0
    direction = 1 if values[0] > 0 else -1 if values[0] < 0 else 0
    if direction == 0:
        return 0
    count = 0
    for value in values:
        if (value > 0 and direction == 1) or (value < 0 and direction == -1):
            count += 1
        else:
            break
    return count * direction

def get_chip_signals(stock_id):
    market = get_stock_market(stock_id)
    records = fetch_institutional_data(stock_id, market)
    signals = []
    score = 0
    details = []

    if not records:
        return signals, score, ["籌碼資料暫時無法取得"]

    latest = records[0]
    foreign_values = [item["foreign"] for item in records]
    trust_values = [item["trust"] for item in records]
    total_values = [item["total"] for item in records]
    foreign_lots = shares_to_lots(latest["foreign"])
    trust_lots = shares_to_lots(latest["trust"])
    total_lots = shares_to_lots(latest["total"])
    total_5d_lots = shares_to_lots(sum(total_values))

    details.append(
        f"最新三大法人：外資 {foreign_lots:+.1f} 張、投信 {trust_lots:+.1f} 張、合計 {total_lots:+.1f} 張"
    )
    details.append(f"近 5 日法人合計：{total_5d_lots:+.1f} 張")

    foreign_streak = consecutive_direction(foreign_values)
    trust_streak = consecutive_direction(trust_values)
    if foreign_streak >= 3:
        signals.append("外資連續買超")
        score += 2
    elif foreign_streak <= -3:
        signals.append("外資連續賣超")
        score -= 2
    elif latest["foreign"] > 0:
        signals.append("外資買超")
        score += 1
    elif latest["foreign"] < 0:
        signals.append("外資賣超")
        score -= 1

    if trust_streak >= 2:
        signals.append("投信連續買超")
        score += 1
    elif trust_streak <= -2:
        signals.append("投信連續賣超")
        score -= 1

    if total_5d_lots >= 5000:
        signals.append("近 5 日法人大幅買超")
        score += 1
    elif total_5d_lots <= -5000:
        signals.append("近 5 日法人大幅賣超")
        score -= 1

    if market == "上市":
        margin = fetch_margin_data(stock_id)
        if margin:
            margin_change = margin["margin_change"]
            short_change = margin["short_change"]
            details.append(
                f"融資變化 {margin_change:+d} 張、融券變化 {short_change:+d} 張"
            )
            if margin_change > 500:
                signals.append("融資增加")
                score -= 1
            elif margin_change < -500:
                signals.append("融資減少")
                score += 1
            if short_change > 100:
                signals.append("融券增加")
                score -= 1
            elif short_change < -100:
                signals.append("融券減少")
                score += 1

    return signals, score, details

def classify_trend(row):
    if pd.isna(row.get("ADX_14")) or pd.isna(row.get("MA60")):
        return "盤整"
    close, ma20, ma60, adx = row["Close"], row["MA20"], row["MA60"], row["ADX_14"]
    if adx >= ADX_TREND_THRESHOLD:
        if close > ma20 > ma60:
            return "強勢多頭"
        if close < ma20 < ma60:
            return "強勢空頭"
    if close > ma60:
        return "偏多"
    if close < ma60:
        return "偏空"
    return "盤整"

def get_effective_volume(df):
    for offset in range(1, 6):
        row = df.iloc[-offset]
        volume = int(row["Volume"])
        if volume > 0:
            return volume, offset
    return 0, None


def get_last_valid_row(df):
    """取得最後一筆有有效收盤價的資料列"""
    for i in range(len(df)):
        row = df.iloc[-(i + 1)]
        if not pd.isna(row["Close"]) and row["Close"] > 0:
            return -(i + 1), row
    return -1, df.iloc[-1]

def get_bb_columns(df):
    return (
        next(col for col in df.columns if col.startswith("BBL_")),
        next(col for col in df.columns if col.startswith("BBM_")),
        next(col for col in df.columns if col.startswith("BBU_")),
    )

def get_kd_columns(df):
    k_col = next(col for col in df.columns if col.startswith("STOCHk_"))
    d_col = next(col for col in df.columns if col.startswith("STOCHd_"))
    return k_col, d_col

def compute_indicators(df):
    kd = ta_lib.stoch(high=df["High"], low=df["Low"], close=df["Close"], k=9, d=3, smooth_k=3)
    df = pd.concat([df, kd], axis=1)
    df["RSI"] = ta_lib.rsi(close=df["Close"], length=14)

    macd = ta_lib.macd(close=df["Close"], fast=12, slow=26, signal=9)
    df = pd.concat([df, macd], axis=1)

    df["MA5"] = ta_lib.sma(close=df["Close"], length=5)
    df["SMA20"] = ta_lib.sma(close=df["Close"], length=20)
    df["SMA60"] = ta_lib.sma(close=df["Close"], length=60)
    df["MA20"] = df["SMA20"]
    df["MA60"] = df["SMA60"]

    bbands = ta_lib.bbands(close=df["Close"], length=20, std=2)
    df = pd.concat([df, bbands], axis=1)

    adx = ta_lib.adx(df["High"], df["Low"], df["Close"], length=14)
    df = pd.concat([df, adx], axis=1)

    df["VOL_MA5"] = ta_lib.sma(close=df["Volume"], length=5)
    df["VOL_MA20"] = ta_lib.sma(close=df["Volume"], length=20)
    df["VOL_RATIO"] = df["Volume"] / df["VOL_MA20"]
    df["TREND"] = df.apply(classify_trend, axis=1)
    return df

def is_strong_bear(row):
    if pd.isna(row.get("ADX_14")) or pd.isna(row.get("SMA20")):
        return False
    return (
        row["ADX_14"] >= ADX_TREND_THRESHOLD
        and row["Close"] < row["SMA20"] < row["SMA60"]
    )

def evaluate_multi_factor_strategy(df):
    """四大面向綜合評分策略
    
    回傳：
        score: 總分
        signal: 最終訊號文字
        details: 各面向明細
        triggered_items: 觸發的細項列表
    """
    k_col, d_col = get_kd_columns(df)
    bbl_col, bbm_col, _ = get_bb_columns(df)
    latest = df.iloc[-1]
    prev = df.iloc[-2]

    close = latest["Close"]
    rsi = latest["RSI"]
    k_value = latest[k_col]
    d_value = latest[d_col]
    prev_k = prev[k_col]
    prev_d = prev[d_col]
    volume, _ = get_effective_volume(df)
    vol_ma5 = latest["VOL_MA5"]
    vol_ma20 = latest["VOL_MA20"]
    sma20 = latest["SMA20"]
    sma60 = latest["SMA60"]
    macd = latest["MACD_12_26_9"]
    macd_signal = latest["MACDs_12_26_9"]
    macd_hist = latest["MACDh_12_26_9"]
    prev_macd = prev["MACD_12_26_9"]
    prev_macd_signal = prev["MACDs_12_26_9"]
    prev_macd_hist = prev["MACDh_12_26_9"]

    score = 0
    triggered_items = []
    details = {"均線買入訊號": [], "技術指標買入訊號": [], "K線量價買入訊號": [], "籌碼面買入訊號": []}

    # ─── 一、均線買入訊號（葛蘭碧八大法則）───
    # 1. 突破均線：均線從下跌轉平緩或往上，股價由下往上突破均線
    ma_trend_up = sma20 > sma60  # 中期均線在長期均線之上
    if close > sma60 and ma_trend_up:
        score += 2
        triggered_items.append("📈 葛蘭碧突破：股價突破季線且均線多頭")
        details["均線買入訊號"].append("突破均線：股價突破季線且均線多頭排列")

    # 2. 回踩支撐：股價在均線之上，拉回未跌破均線後反彈
    if close > sma20 > sma60 and prev["Close"] <= sma20 and close > sma20:
        score += 2
        triggered_items.append("📈 葛蘭碧回踩：股價回測20日線不破反彈")
        details["均線買入訊號"].append("回踩支撐：股價回測20日線不破反彈")

    # 3. 假跌破：股價跌破上升中的均線，隨即又漲回
    if prev["Close"] < sma20 and close > sma20 and sma20 > prev["SMA20"]:
        score += 2
        triggered_items.append("📈 葛蘭碧假跌破：跌破20日線後立即站回")
        details["均線買入訊號"].append("假跌破：股價跌破上升20日線後立即站回")

    # 4. 負乖離過大：股價跌破均線且嚴重偏離，有跌深反彈機會
    bbm_val = latest[bbm_col]
    bbl_val = latest[bbl_col]
    neg_deviation = (bbm_val - close) / bbm_val  # 偏離中線比例
    if close < bbl_val and neg_deviation > 0.05:
        score += 1
        triggered_items.append("📈 負乖離過大：股價跌破布林下軌")
        details["均線買入訊號"].append("負乖離過大：股價跌破布林下軌，乖離率>5%")

    # ─── 二、技術指標買入訊號 ───
    # 5. MACD 金叉：快線突破慢線，且柱狀圖由負轉正
    macd_bull_cross = prev_macd <= prev_macd_signal and macd > macd_signal
    macd_hist_pos = macd_hist > 0 and prev_macd_hist <= 0
    if macd_bull_cross:
        score += 2
        triggered_items.append("📊 MACD 黃金交叉")
        details["技術指標買入訊號"].append("MACD 黃金交叉：DIF突破MACD")
    if macd_hist_pos:
        score += 1
        triggered_items.append("📊 MACD 柱狀體由負轉正")
        details["技術指標買入訊號"].append("MACD 柱狀體轉正")

    # 6. KD 低檔黃金交叉：K值<20由下往上突破D值
    if k_value > d_value and prev_k <= prev_d and k_value < 20:
        score += 2
        triggered_items.append("📊 KD 低檔黃金交叉 (K<20)")
        details["技術指標買入訊號"].append("KD 低檔黃金交叉：K值<20由下往上突破D值")
    elif k_value > d_value and prev_k <= prev_d and k_value < 40:
        score += 1
        triggered_items.append("📊 KD 黃金交叉 (K<40)")
        details["技術指標買入訊號"].append("KD 黃金交叉：K值<40由下往上突破D值")

    # 7. RSI 超賣區回升：RSI<20後開始向上反彈
    if rsi > 30 and prev["RSI"] < 30:
        score += 2
        triggered_items.append("📊 RSI 超賣區回升")
        details["技術指標買入訊號"].append("RSI 超賣區回升：RSI從30以下反彈")
    elif rsi < 20 and rsi > prev["RSI"]:
        score += 1
        triggered_items.append("📊 RSI 深超賣反彈")
        details["技術指標買入訊號"].append("RSI 深超賣區反彈")

    # ─── 三、K線量價買入訊號 ───
    # 8. 價漲量增：股價上漲時成交量放大
    if close > prev["Close"] and vol_ma20 > 0 and volume > vol_ma20 * 1.3:
        score += 2
        triggered_items.append("💰 價漲量增：漲幅配合1.3倍均量")
        details["K線量價買入訊號"].append("價漲量增：股價上漲且成交量>20日均量1.3倍")

    # 9. 低檔大陽線：長期下跌後，低點爆量長紅K
    price_change = (close - prev["Close"]) / prev["Close"]
    if price_change > 0.03 and volume > vol_ma20 * 1.5 and sma20 < sma60:
        score += 2
        triggered_items.append("💰 低檔大陽線：跌幅後爆量長紅")
        details["K線量價買入訊號"].append("低檔大陽線：長期下跌後爆量長紅K(漲幅>3%)")

    # 10. 突破盤整區：帶量突破長期橫盤整理平台
    high_20 = df["High"].rolling(20).max()
    if close >= high_20.iloc[-2] and volume > vol_ma20 * 1.3:
        score += 2
        triggered_items.append("💰 突破盤整：帶量突破20日高點")
        details["K線量價買入訊號"].append("突破盤整：股價帶量突破20日高點")

    # ─── 四、籌碼面買入訊號（由外部傳入，此處僅佔位）───
    # 實際籌碼分數會由 get_chip_signals 計算後加入

    # ─── 決定最終訊號 ───
    if score >= SCORE_THRESHOLD_STRONG:
        signal = f"🔥🔥 強烈買入訊號（總分 {score} 分）"
    elif score >= SCORE_THRESHOLD_BUY:
        signal = f"✅ 買入訊號（總分 {score} 分）"
    elif score >= SCORE_THRESHOLD_WATCH:
        signal = f"👀 觀察名單（總分 {score} 分）"
    else:
        signal = f"⚪ 觀望（總分 {score} 分）"

    return {
        "score": score,
        "signal": signal,
        "is_recommended": score >= SCORE_THRESHOLD_BUY,
        "rec_type": "strong" if score >= SCORE_THRESHOLD_STRONG else ("normal" if score >= SCORE_THRESHOLD_BUY else None),
        "is_watch": SCORE_THRESHOLD_WATCH <= score < SCORE_THRESHOLD_BUY,
        "triggered_items": triggered_items,
        "details": details,
        "values": {
            "close": round(close, 2),
            "sma60": round(sma60, 2),
            "sma20": round(sma20, 2),
            "macd_hist": round(macd_hist, 4),
            "prev_macd_hist": round(prev_macd_hist, 4),
            "rsi": round(rsi, 2),
            "k": round(k_value, 2),
            "d": round(d_value, 2),
            "volume": int(volume),
            "vol_ma5": int(vol_ma5) if vol_ma5 else 0,
            "vol_ma20": int(vol_ma20) if vol_ma20 else 0,
        },
    }


def get_buy_recommendation(strategy_result, up_prob):
    """根據綜合評分 + 勝率決定推薦等級"""
    is_rec = strategy_result.get("is_recommended", False)
    rec_type = strategy_result.get("rec_type")
    score = strategy_result.get("score", 0)

    if is_rec and up_prob >= RECOMMEND_MIN_WIN_RATE:
        return True, rec_type or "normal"
    if is_rec:
        return True, "watch"
    return False, None


def format_signal_board(result):
    """格式化顯示綜合評分結果"""
    lines = []
    lines.append("\n📊 四大面向綜合評分")
    lines.append("────────────────────")
    lines.append(f"  總分：{result['score']} / {SCORE_THRESHOLD_STRONG}（強烈買入門檻）")
    lines.append(f"  訊號：{result['signal']}")
    lines.append("")
    for category, items in result["details"].items():
        if items:
            lines.append(f"  {category}：")
            for item in items:
                lines.append(f"    ✅ {item}")
    if result["triggered_items"]:
        lines.append("\n  📋 觸發項目一覽：")
        for item in result["triggered_items"]:
            lines.append(f"    {item}")
    return "\n".join(lines)


def get_rec_type_label(rec_type):
    labels = {
        "strong": "🔥 強烈買入",
        "normal": "✅ 買入",
        "watch": "👀 觀察",
    }
    return labels.get(rec_type, "推薦")

def get_trend_filter(df):
    latest = df.iloc[-1]
    trend = latest["TREND"]
    adx = round(latest["ADX_14"], 2) if not pd.isna(latest["ADX_14"]) else 0
    notes = []
    allow_counter_buy = True
    allow_counter_sell = True

    if trend == "強勢多頭":
        notes.append("強勢多頭趨勢，RSI/KD 超買為常態，不視為賣出訊號")
        allow_counter_sell = False
    elif trend == "強勢空頭":
        notes.append("強勢空頭趨勢，RSI/KD 超賣可能鈍化，避免逆勢接刀")
        allow_counter_buy = False
    elif trend == "偏多":
        notes.append("中期偏多，超買訊號需搭配量能確認")
        allow_counter_sell = False
    elif trend == "偏空":
        notes.append("中期偏空，超賣反彈需謹慎看待")
        allow_counter_buy = False
    else:
        notes.append("盤整格局，KD/RSI 訊號參考價值較高")

    return {
        "trend": trend,
        "adx": adx,
        "notes": notes,
        "allow_counter_buy": allow_counter_buy,
        "allow_counter_sell": allow_counter_sell,
    }

def get_technical_signals(df, trend_filter):
    bb_upper_col = next(col for col in df.columns if col.startswith("BBU_"))
    bb_lower_col = next(col for col in df.columns if col.startswith("BBL_"))
    k_col, d_col = get_kd_columns(df)
    latest = df.iloc[-1]
    prev = df.iloc[-2]
    allow_buy = trend_filter["allow_counter_buy"]
    allow_sell = trend_filter["allow_counter_sell"]

    signals = []
    score = 0
    values = {
        "k": round(latest[k_col], 2),
        "d": round(latest[d_col], 2),
        "rsi": round(latest["RSI"], 2),
        "macd": round(latest["MACD_12_26_9"], 4),
        "macd_signal": round(latest["MACDs_12_26_9"], 4),
        "macd_hist": round(latest["MACDh_12_26_9"], 4),
        "ma5": round(latest["MA5"], 2),
        "ma20": round(latest["MA20"], 2),
        "ma60": round(latest["MA60"], 2),
        "bb_upper": round(latest[bb_upper_col], 2),
        "bb_lower": round(latest[bb_lower_col], 2),
        "close": round(latest["Close"], 2),
        "adx": trend_filter["adx"],
        "trend": trend_filter["trend"],
    }

    volume, volume_offset = get_effective_volume(df)
    vol_ma20 = int(latest["VOL_MA20"]) if latest["VOL_MA20"] > 0 else int(df.iloc[-volume_offset]["VOL_MA20"]) if volume_offset else 0
    vol_ratio = round(volume / vol_ma20, 2) if vol_ma20 > 0 else None
    values["volume"] = volume
    values["vol_ma20"] = vol_ma20
    values["vol_ratio"] = vol_ratio

    if prev[k_col] <= prev[d_col] and values["k"] > values["d"]:
        signals.append(("KD 黃金交叉", 2, "cross"))
        score += 2
    elif prev[k_col] >= prev[d_col] and values["k"] < values["d"]:
        signals.append(("KD 死亡交叉", -2, "cross"))
        score -= 2

    if values["k"] < 20 and values["d"] < 20:
        if allow_buy:
            signals.append(("KD 超賣區", 1, "counter"))
            score += 1
        else:
            signals.append(("KD 超賣區（趨勢過濾忽略）", 0, "filtered"))
    elif values["k"] > 80 and values["d"] > 80:
        if allow_sell:
            signals.append(("KD 超買區", -1, "counter"))
            score -= 1
        else:
            signals.append(("KD 超買區（趨勢過濾忽略）", 0, "filtered"))

    if values["rsi"] < 30:
        if allow_buy:
            signals.append(("RSI 超賣", 1, "counter"))
            score += 1
        else:
            signals.append(("RSI 超賣（趨勢過濾忽略）", 0, "filtered"))
    elif values["rsi"] > 70:
        if allow_sell:
            signals.append(("RSI 超買", -1, "counter"))
            score -= 1
        else:
            signals.append(("RSI 超買（趨勢過濾忽略）", 0, "filtered"))

    if prev["MACD_12_26_9"] <= prev["MACDs_12_26_9"] and values["macd"] > values["macd_signal"]:
        signals.append(("MACD 黃金交叉", 2, "cross"))
        score += 2
    elif prev["MACD_12_26_9"] >= prev["MACDs_12_26_9"] and values["macd"] < values["macd_signal"]:
        signals.append(("MACD 死亡交叉", -2, "cross"))
        score -= 2

    if values["ma5"] > values["ma20"] > values["ma60"]:
        signals.append(("均線多頭排列", 2, "trend"))
        score += 2
    elif values["ma5"] < values["ma20"] < values["ma60"]:
        signals.append(("均線空頭排列", -2, "trend"))
        score -= 2
    elif prev["MA5"] <= prev["MA20"] and values["ma5"] > values["ma20"]:
        signals.append(("MA5 上穿 MA20", 1, "cross"))
        score += 1
    elif prev["MA5"] >= prev["MA20"] and values["ma5"] < values["ma20"]:
        signals.append(("MA5 下穿 MA20", -1, "cross"))
        score -= 1

    if values["close"] <= values["bb_lower"]:
        if allow_buy:
            signals.append(("觸及布林下軌", 1, "counter"))
            score += 1
        else:
            signals.append(("觸及布林下軌（趨勢過濾忽略）", 0, "filtered"))
    elif values["close"] >= values["bb_upper"]:
        if allow_sell:
            signals.append(("觸及布林上軌", -1, "counter"))
            score -= 1
        else:
            signals.append(("觸及布林上軌（趨勢過濾忽略）", 0, "filtered"))

    return signals, score, values

def apply_volume_confirmation(signals, score, vol_ratio):
    if vol_ratio is None:
        return [name for name, _, _ in signals], score, ["量能資料不足，交叉訊號未確認"]

    notes = [f"量能比：{vol_ratio:.0%}（相對 20 日均量）"]
    adjusted_score = score
    confirmed = []

    if vol_ratio >= VOLUME_STRONG_RATIO:
        notes.append("量能充沛，主力表態明確")
        cross_bonus = sum(1 for _, pts, kind in signals if kind == "cross" and pts > 0)
        cross_penalty = sum(1 for _, pts, kind in signals if kind == "cross" and pts < 0)
        adjusted_score += cross_bonus
        adjusted_score -= cross_penalty
    elif vol_ratio >= VOLUME_CONFIRM_RATIO:
        notes.append("量能達標，交叉訊號有效")
    elif vol_ratio >= VOLUME_LOW_RATIO:
        notes.append("量能偏弱，交叉訊號可信度降低")
        for name, pts, kind in signals:
            if kind == "cross" and pts != 0:
                adjusted_score -= pts // 2
                confirmed.append(f"{name}（量能不足打折）")
            else:
                confirmed.append(name)
        return confirmed, adjusted_score, notes
    else:
        notes.append("量能極低，交叉訊號視為假訊號")
        for name, pts, kind in signals:
            if kind == "cross" and pts != 0:
                adjusted_score -= pts
                confirmed.append(f"{name}（假訊號已剔除）")
            else:
                confirmed.append(name)
        return confirmed, adjusted_score, notes

    confirmed = [name for name, _, _ in signals]
    return confirmed, adjusted_score, notes

def get_action_from_score(score):
    if score >= 4:
        return "強烈買進訊號"
    if score >= 2:
        return "買進訊號"
    if score <= -4:
        return "強烈賣出訊號"
    if score <= -2:
        return "賣出訊號"
    if score > 0:
        return "偏多（弱買進）"
    if score < 0:
        return "偏空（弱賣出）"
    return "觀望"

def estimate_2week_probability(df, total_score, trend_state):
    k_col, _ = get_kd_columns(df)
    current = df.iloc[-1]
    cur_k, cur_rsi = current[k_col], current["RSI"]
    cur_trend = trend_state
    cur_vol_ratio = current["VOL_RATIO"] if not pd.isna(current["VOL_RATIO"]) else 1.0

    up_count = 0
    total = 0
    returns = []

    for i in range(len(df) - FORECAST_DAYS - 1):
        row = df.iloc[i]
        if pd.isna(row[k_col]) or pd.isna(row["RSI"]) or pd.isna(row["TREND"]):
            continue
        if abs(row[k_col] - cur_k) > 15 or abs(row["RSI"] - cur_rsi) > 10:
            continue
        if row["TREND"] != cur_trend:
            continue
        if not pd.isna(row["VOL_RATIO"]):
            if abs(row["VOL_RATIO"] - cur_vol_ratio) > 0.8:
                continue

        past_close = row["Close"]
        future_close = df.iloc[i + FORECAST_DAYS]["Close"]
        if past_close <= 0:
            continue

        total += 1
        ret = (future_close - past_close) / past_close
        returns.append(ret)
        if ret > 0:
            up_count += 1

    data_years = len(df) / 252
    if total >= 10:
        up_prob = round(up_count / total * 100, 1)
        avg_return = round(sum(returns) / len(returns) * 100, 2)
        method = f"{data_years:.0f} 年大歷史回測（{total} 次相似樣本，趨勢：{cur_trend}）"
        return up_prob, round(100 - up_prob, 1), method, avg_return, total

    up_count = 0
    total = 0
    returns = []
    for i in range(len(df) - FORECAST_DAYS - 1):
        row = df.iloc[i]
        if pd.isna(row[k_col]) or pd.isna(row["RSI"]):
            continue
        if abs(row[k_col] - cur_k) > 15 or abs(row["RSI"] - cur_rsi) > 10:
            continue
        past_close = row["Close"]
        future_close = df.iloc[i + FORECAST_DAYS]["Close"]
        if past_close <= 0:
            continue
        total += 1
        ret = (future_close - past_close) / past_close
        returns.append(ret)
        if ret > 0:
            up_count += 1

    if total >= 5:
        up_prob = round(up_count / total * 100, 1)
        avg_return = round(sum(returns) / len(returns) * 100, 2)
        method = f"{data_years:.0f} 年大歷史回測（{total} 次，放寬趨勢條件）"
        return up_prob, round(100 - up_prob, 1), method, avg_return, total

    base_up = 50 + total_score * 3
    up_prob = round(max(15, min(85, base_up)), 1)
    method = f"{data_years:.0f} 年資料不足，改以綜合評分估算"
    return up_prob, round(100 - up_prob, 1), method, None, total

def analyze_stock_from_df(stock_id, category, df, skip_probability=False):
    if df is None or df.empty or len(df) < FORECAST_DAYS + 60:
        return None

    # 移除尾端 NaN 收盤價（Yahoo 盤中尚未更新的資料）
    df = df.dropna(subset=["Close"])
    if df.empty or len(df) < FORECAST_DAYS + 60:
        return None

    df = compute_indicators(df)
    trend_filter = get_trend_filter(df)
    strategy_result = evaluate_multi_factor_strategy(df)

    if skip_probability:
        up_prob, down_prob, prob_method, avg_return, sample_count = (
            -1, -1, "快速掃描（待計算勝率）", None, 0
        )
        is_recommended, rec_type = False, None
    else:
        up_prob, down_prob, prob_method, avg_return, sample_count = estimate_2week_probability(
            df, 0, trend_filter["trend"]
        )
        is_recommended, rec_type = get_buy_recommendation(strategy_result, up_prob)

    score = strategy_result["score"]
    latest = df.iloc[-1]
    return {
        "stock_id": stock_id,
        "stock_name": get_stock_name(stock_id),
        "category": category,
        "close": round(latest["Close"], 2),
        "date": latest.name.strftime("%Y-%m-%d"),
        "score": score,
        "is_recommended": is_recommended,
        "rec_type": rec_type,
        "is_watch": strategy_result.get("is_watch", False),
        "up_prob": up_prob,
        "down_prob": down_prob,
        "avg_return": avg_return,
        "sample_count": sample_count,
        "prob_method": prob_method,
        "trend": trend_filter["trend"],
        "final_signal": strategy_result["signal"],
        "triggered_items": strategy_result["triggered_items"],
        "strategy_details": strategy_result["details"],
    }

def fetch_stock_histories_batch(stock_ids, period=SCAN_FAST_PERIOD):
    if not stock_ids:
        return {}

    symbol_map = {get_yfinance_symbol(stock_id): stock_id for stock_id in stock_ids}
    symbols = list(symbol_map.keys())
    histories = {}

    try:
        data = yf.download(
            symbols,
            period=period,
            group_by="ticker",
            threads=True,
            progress=False,
            auto_adjust=True,
        )
    except Exception:
        data = None

    if data is None or data.empty:
        return histories

    if len(symbols) == 1:
        symbol = symbols[0]
        df = data.dropna(how="all")
        if not df.empty:
            histories[symbol_map[symbol]] = df
        return histories

    for symbol in symbols:
        try:
            df = data[symbol].dropna(how="all")
            if not df.empty:
                histories[symbol_map[symbol]] = df
        except (KeyError, TypeError, AttributeError):
            continue
    return histories

def sort_results_by_win_rate(results):
    return sorted(
        results,
        key=lambda x: (-x.get("up_prob", -1), x["stock_id"]),
    )

def analyze_stock_for_scan(stock_id, category, df=None, skip_probability=False):
    if df is None:
        symbol = get_yfinance_symbol(stock_id)
        period = SCAN_FAST_PERIOD if skip_probability else BACKTEST_PERIOD
        try:
            df = yf.Ticker(symbol).history(period=period)
        except Exception:
            return None
    return analyze_stock_from_df(stock_id, category, df, skip_probability=skip_probability)

def format_strategy_status(result):
    return format_five_strategy_status(result)

SCAN_CSV_COLUMNS = [
    "rank",
    "stock_id",
    "stock_name",
    "market",
    "close",
    "date",
    "A_多頭回檔",
    "B_動能突破",
    "C_超賣反彈",
    "原策略三",
    "經典1+3",
    "五大策略",
    "up_prob",
    "is_recommended",
    "rec_type",
    "trend",
    "final_signal",
]

def result_to_csv_row(result, rank=None):
    up_prob = result["up_prob"]
    row = {
        "rank": rank if rank is not None else "",
        "stock_id": result["stock_id"],
        "stock_name": result["stock_name"],
        "market": result["category"],
        "close": result["close"],
        "date": result["date"],
        "A_多頭回檔": "○" if result.get("strategy_1_buy") else "✗",
        "B_動能突破": "○" if result.get("strategy_2_buy") else "✗",
        "C_超賣反彈": "○" if result.get("strategy_3_buy") else "✗",
        "原策略三": "○" if result.get("legacy_s3_alone_buy") else "✗",
        "經典1+3": "○" if result.get("legacy_1_3_buy") else "✗",
        "五大策略": format_five_strategy_status(result),
        "up_prob": up_prob if up_prob >= 0 else "",
        "is_recommended": result["is_recommended"],
        "rec_type": result.get("rec_type") or "",
        "trend": result["trend"],
        "final_signal": result["final_signal"],
    }
    return row

def save_scan_results_csv(filepath, results, sort_by_win_rate=False):
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    if sort_by_win_rate:
        results = sort_results_by_win_rate(results)
    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=SCAN_CSV_COLUMNS)
        writer.writeheader()
        for idx, result in enumerate(results, 1):
            rank = idx if sort_by_win_rate and result.get("up_prob", -1) >= 0 else ""
            writer.writerow(result_to_csv_row(result, rank=rank))

def format_full_market_report(scan_date, all_results, triggered, failed_ids, file_paths):
    total = len(all_results) + len(failed_ids)
    lines = [
        f"📅 {scan_date} 全市場掃描",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🔍 共 {total} 檔｜推薦 {len(triggered)} 檔｜失敗 {len(failed_ids)} 檔",
        "",
    ]

    if triggered:
        lines.append("🔥 推薦名單")
        lines.append("股票代號 股票名稱       評分 買入訊號           2年回測勝率(觸發次數)")
        lines.append("──────────────────────────────────────────")
        for item in sort_results_by_win_rate(triggered):
            sid = item['stock_id']
            name = item['stock_name']
            score = item.get('score', 0)
            up = item.get('up_prob', -1)
            cnt = item.get('sample_count', 0)
            sig = "🔴強買" if score >= 10 else ("🟢買入" if score >= 6 else ("🟡觀察" if score >= 4 else "⚪觀望"))
            up_str = f"{up}%" if up >= 0 else "-"
            cnt_str = f"({cnt})" if cnt else ""
            lines.append(f"{sid} {name:<6s}  {score}分 {sig:<6s}  {up_str:>4s}{cnt_str}")
        lines.append("")
    else:
        lines.append("🔥 推薦名單（0 檔）")
        lines.append("")

    lines.append("⚠️ 僅供參考，非投資建議。")
    return "\n".join(lines)

def enrich_triggered_results(triggered):
    if not triggered:
        return []

    enriched = []
    total = len(triggered)
    print(f"\n第二階段：為 {total} 檔觸發股票計算完整勝率（5 年回測）...")

    def enrich_one(item):
        stock_id = item["stock_id"]
        return analyze_stock_for_scan(stock_id, item["category"], skip_probability=False)

    with ThreadPoolExecutor(max_workers=FULL_SCAN_WORKERS) as executor:
        futures = {executor.submit(enrich_one, item): item for item in triggered}
        done = 0
        for future in as_completed(futures):
            done += 1
            if done % 20 == 0 or done == total:
                print(f"  勝率計算進度：{done}/{total}")
            try:
                result = future.result()
            except Exception:
                result = None
            if result is not None:
                enriched.append(result)

    return sort_results_by_win_rate(enriched)

def scan_all_stocks_parallel(stock_ids, progress_step=100):
    all_results = []
    triggered_ids = set()
    failed_ids = []
    total = len(stock_ids)
    batch_count = (total + FULL_SCAN_BATCH_SIZE - 1) // FULL_SCAN_BATCH_SIZE

    print(
        f"第一階段：快速策略掃描（{FULL_SCAN_WORKERS} 執行緒，"
        f"每批 {FULL_SCAN_BATCH_SIZE} 檔，共 {batch_count} 批）...\n"
    )

    completed = 0
    for batch_idx in range(batch_count):
        start = batch_idx * FULL_SCAN_BATCH_SIZE
        batch_ids = stock_ids[start : start + FULL_SCAN_BATCH_SIZE]
        histories = fetch_stock_histories_batch(batch_ids, period=SCAN_FAST_PERIOD)

        missing_ids = [sid for sid in batch_ids if sid not in histories]
        if missing_ids:
            def fetch_one(stock_id):
                return stock_id, analyze_stock_for_scan(
                    stock_id, get_stock_market(stock_id), skip_probability=True
                )

            with ThreadPoolExecutor(max_workers=FULL_SCAN_WORKERS) as executor:
                for stock_id, result in executor.map(fetch_one, missing_ids):
                    completed += 1
                    if result is None:
                        failed_ids.append(stock_id)
                        continue
                    all_results.append(result)
                    if has_any_strategy_trigger(result):
                        triggered_ids.add(stock_id)

        for stock_id in batch_ids:
            if stock_id in missing_ids:
                continue
            completed += 1
            result = analyze_stock_from_df(
                stock_id, get_stock_market(stock_id), histories[stock_id], skip_probability=True
            )
            if result is None:
                failed_ids.append(stock_id)
                continue
            all_results.append(result)
            if has_any_strategy_trigger(result):
                triggered_ids.add(stock_id)

        if completed % progress_step < FULL_SCAN_BATCH_SIZE or completed == total:
            print(f"進度：{completed}/{total}（已發現觸發 {len(triggered_ids)} 檔）")

    fast_triggered = [r for r in all_results if r["stock_id"] in triggered_ids]
    triggered = enrich_triggered_results(fast_triggered)

    enriched_map = {item["stock_id"]: item for item in triggered}
    all_results = [
        enriched_map.get(item["stock_id"], item) for item in all_results
    ]
    all_results = sort_results_by_win_rate(all_results)
    return all_results, triggered, failed_ids

def full_market_scanner():
    scan_date = datetime.now().strftime("%Y-%m-%d")
    stock_ids = get_all_tw_stock_ids()
    print(f"開始全市場掃描（{scan_date}）...")
    print(f"共 {len(stock_ids)} 檔上市櫃普通股\n")

    all_results, triggered, failed_ids = scan_all_stocks_parallel(stock_ids)

    os.makedirs(SCAN_RESULTS_DIR, exist_ok=True)
    all_path = os.path.join(SCAN_RESULTS_DIR, f"full_scan_{scan_date}.csv")
    triggered_path = os.path.join(SCAN_RESULTS_DIR, f"triggered_{scan_date}.csv")
    save_scan_results_csv(all_path, all_results, sort_by_win_rate=True)
    save_scan_results_csv(triggered_path, triggered, sort_by_win_rate=True)

    for item in triggered:
        print(
            f"  ○ {item['stock_id']} {item['stock_name']} "
            f"[{format_five_strategy_status(item)}] 勝率 {item['up_prob']}%"
        )

    report = format_full_market_report(
        scan_date,
        all_results,
        triggered,
        failed_ids,
        {"all": all_path, "triggered": triggered_path},
    )
    print("\n" + report)
    return report

def format_daily_line_report(
    scan_date,
    recommendations,
    all_results,
    failed_ids,
    trending_info=None,
    trending_triggered=None,
):
    lines = [
        f"📅 {scan_date} 台股每日掃描",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    # 推薦進場名單
    MAX_SHOW = 15
    if recommendations:
        show_list = recommendations[:MAX_SHOW]
        lines.append(f"🔥 推薦名單（共 {len(recommendations)} 檔）")
        lines.append("股票代號 股票名稱       評分 買入訊號           2年回測勝率(觸發次數)")
        lines.append("──────────────────────────────────────────")
        for item in show_list:
            sid = item['stock_id']
            name = item['stock_name']
            score = item.get('score', 0)
            rec_type = item.get('rec_type', '')
            up = item.get('up_prob', -1)
            cnt = item.get('sample_count', 0)
            sig = "🔴強買" if score >= 10 else ("🟢買入" if score >= 6 else ("🟡觀察" if score >= 4 else "⚪觀望"))
            up_str = f"{up}%" if up >= 0 else "-"
            cnt_str = f"({cnt})" if cnt else ""
            lines.append(f"{sid} {name:<6s}  {score}分 {sig:<6s}  {up_str:>4s}{cnt_str}")
        lines.append("")
    else:
        lines.extend([f"🔥 推薦名單（0 檔）", "　今日無股票符合條件", ""])

    # 網路熱門提及
    if trending_info and trending_info["top_5_mentioned"]:
        lines.append("🌐 網路熱門 TOP 5")
        for stock_id, count in trending_info["top_5_mentioned"]:
            lines.append(f"　・{stock_id} {get_stock_name(stock_id)}｜提及 {count} 次")
        lines.append("")

    # 掃描摘要（簡化）
    total = len(all_results) + len(failed_ids)
    lines.append("📊 摘要")
    lines.append(f"　掃描 {total} 檔｜推薦 {len(recommendations)} 檔｜失敗 {len(failed_ids)} 檔")
    if trending_info:
        names = [f"{sid}" for sid in trending_info['new_trending_stocks']]
        if names:
            lines.append(f"　焦點新股：{' '.join(names)}")
    if failed_ids:
        lines.append(f"　失敗：{' '.join(failed_ids)}")
    lines.append("")
    lines.append("⚠️ 僅供參考，非投資建議。")

    return "\n".join(lines)

def scan_stock_list(stock_map, all_results, recommendations, trending_triggered, failed_ids):
    for category, stock_ids in stock_map.items():
        print(f"▶ 掃描：{category}")
        for stock_id in stock_ids:
            print(f"  分析 {stock_id}...", end=" ", flush=True)
            result = analyze_stock_for_scan(stock_id, category)
            if result is None:
                print("失敗")
                failed_ids.append(stock_id)
                continue

            result["is_trending"] = category == "今日網路焦點新股"
            all_results.append(result)

            score = result.get("score", 0)
            triggered = score >= SCORE_THRESHOLD_BUY
            watch = score >= SCORE_THRESHOLD_WATCH
            trigger_str = f"評分 {score}分"

            if result["is_trending"] and triggered:
                print(f"🌐 焦點觸發！[{trigger_str}] 勝率 {result['up_prob']}%")
                trending_triggered.append(result)
            elif result["is_recommended"]:
                label = get_rec_type_label(result.get("rec_type"))
                print(f"🔥 {label}！[{trigger_str}] 勝率 {result['up_prob']}%")
                recommendations.append(result)
            elif watch:
                print(f"👀 觀察 [{trigger_str}] 勝率 {result['up_prob']}%")
            else:
                print(f"略過 [{trigger_str}] 勝率 {result['up_prob']}%")

def daily_stock_scanner():
    scan_date = datetime.now().strftime("%Y-%m-%d")
    print(f"開始每日掃描（{scan_date}）...\n")

    print("🌐 正在偵測 PTT 股版與 Yahoo 財經熱門話題...")
    trending_info = fetch_trending_stocks()
    new_trending_stocks = trending_info["new_trending_stocks"]

    if trending_info["top_5_mentioned"]:
        print("　熱門提及 TOP 5：")
        for stock_id, count in trending_info["top_5_mentioned"]:
            print(f"   - {stock_id} {get_stock_name(stock_id)}（{count} 次）")
    else:
        print("　今日尚未擷取到有效熱門股票代號")

    print(f"　焦點新股（不在主流池）：{', '.join(new_trending_stocks) or '無'}\n")

    all_results = []
    recommendations = []
    trending_triggered = []
    failed_ids = []

    scan_stock_list(HOT_STOCK_POOL, all_results, recommendations, trending_triggered, failed_ids)

    if new_trending_stocks:
        trending_pool = {"今日網路焦點新股": new_trending_stocks}
        scan_stock_list(trending_pool, all_results, recommendations, trending_triggered, failed_ids)

    recommendations = [r for r in recommendations if not r.get("is_trending")]
    recommendations.sort(key=lambda x: x["up_prob"], reverse=True)
    trending_triggered.sort(key=lambda x: x["up_prob"], reverse=True)

    report = format_daily_line_report(
        scan_date,
        recommendations,
        all_results,
        failed_ids,
        trending_info=trending_info,
        trending_triggered=trending_triggered,
    )
    print("\n" + report)
    return report

def get_stock_analysis(stock_id):
    formatted_id = get_yfinance_symbol(stock_id)
    stock_name = get_stock_name(stock_id)
    df = yf.Ticker(formatted_id).history(period=BACKTEST_PERIOD)

    if df.empty or len(df) < FORECAST_DAYS + 60:
        print(f"❌ {stock_id} {stock_name}：資料不足")
        return

    df = df.dropna(subset=["Close"])
    if df.empty or len(df) < FORECAST_DAYS + 60:
        print(f"❌ {stock_id} {stock_name}：資料不足")
        return

    df = compute_indicators(df)
    strategy_result = evaluate_multi_factor_strategy(df)
    up_prob, down_prob, prob_method, avg_return, sample_count = estimate_2week_probability(
        df, 0, df.iloc[-1]["TREND"]
    )
    score = strategy_result["score"]
    sig = "🔴強買" if score >= 10 else ("🟢買入" if score >= 6 else ("🟡觀察" if score >= 4 else "⚪觀望"))
    date_str = df.iloc[-1].name.strftime("%Y-%m-%d")
    close = df.iloc[-1]["Close"]

    print(f"\n{stock_id} {stock_name}")
    print(f"評分 {score}分 {sig} ｜ 收盤 ${close:.2f} ({date_str})")
    print(f"2年回測勝率 {up_prob}%（觸發 {sample_count} 次）")
    print(f"訊號：{strategy_result['signal']}")
    if strategy_result["triggered_items"]:
        print(f"觸發項目：{'、'.join(strategy_result['triggered_items'][:3])}")

if __name__ == "__main__":
    print("=" * 34)
    print("  台股智能分析機器人")
    print("=" * 34)
    print("1. 單一股票深度分析")
    print("2. 每日熱門股池掃描推薦")
    print("3. 全市場掃描（寫入 CSV，篩選觸發股票）")
    choice = input("\n請選擇功能 (1/2/3)：").strip()

    if choice == "2":
        daily_stock_scanner()
    elif choice == "3":
        full_market_scanner()
    elif choice == "1":
        stock_id = input("請輸入股票代號（例如 2330）：").strip()
        if not stock_id:
            print("未輸入代號，程式結束。")
        else:
            get_stock_analysis(stock_id)
    else:
        print("無效選項，程式結束。")
