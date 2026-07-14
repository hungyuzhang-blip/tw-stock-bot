"""
台股智能分析 LINE Bot
整合 stock_bot.py 現有分析功能，提供 LINE 互動查詢
"""

import os
import sys
import io
import traceback
from datetime import datetime
from threading import Thread

from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from dotenv import load_dotenv

load_dotenv()

# 匯入 stock_bot 分析函式
sys.path.insert(0, os.path.dirname(__file__))
from stock_bot import (
    daily_stock_scanner,
    full_market_scanner,
    get_stock_name,
    is_valid_stock_code,
)

app = Flask(__name__)

# LINE Bot 設定
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)


# ─── 命令解析 ───────────────────────────────────────────

def parse_command(text: str) -> dict:
    """解析使用者輸入，回傳命令類型與參數"""
    t = text.strip()

    if not t:
        return {"type": "help"}

    # 命令關鍵字
    if t in ("說明", "help", "h", "選單", "menu", "?"):
        return {"type": "help"}

    if t in ("掃描", "scan", "每日掃描", "熱門"):
        return {"type": "scan"}

    if t in ("全市場", "full", "full_market"):
        return {"type": "full_market"}

    # 股票代號（純數字 4 碼）
    if t.isdigit() and len(t) == 4:
        return {"type": "stock", "stock_id": t}

    # 股票代號 + TW 後綴相容
    if t.endswith((".TW", ".TWO")):
        sid = t.replace(".TW", "").replace(".TWO", "")
        if sid.isdigit() and len(sid) == 4:
            return {"type": "stock", "stock_id": sid}

    return {"type": "unknown"}


# ─── 回覆產生器 ─────────────────────────────────────────

def build_help_message() -> str:
    return (
        "📌 台股分析機器人 使用說明\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "🔹 查詢單一股票\n"
        "　直接輸入 4 碼股票代號\n"
        "　例如：2330\n\n"
        "🔹 每日熱門股池掃描\n"
        "　輸入：掃描\n\n"
        "🔹 全市場掃描\n"
        "　輸入：全市場\n"
        "　（掃描所有上市櫃股票，需較長時間）\n\n"
        "🔹 顯示此說明\n"
        "　輸入：說明"
    )


def build_stock_reply(stock_id: str) -> str:
    """執行單一股票深度分析並回傳文字報告"""
    try:
        # 檢查股票代號是否有效
        if not is_valid_stock_code(stock_id):
            name = get_stock_name(stock_id)
            return f"❌ 無效的股票代號：{stock_id}\n請輸入正確的台灣上市/上櫃股票代號（例如 2330）"

        # 重新導向 stdout 來捕捉 get_stock_analysis 的輸出
        from stock_bot import get_stock_analysis as run_analysis
        old_stdout = sys.stdout
        redirected_output = io.StringIO()
        sys.stdout = redirected_output

        try:
            run_analysis(stock_id)
        finally:
            sys.stdout = old_stdout

        output = redirected_output.getvalue()

        # LINE 訊息長度限制約 5000 字，若太長則截斷
        if len(output) > 4800:
            output = output[:4800] + "\n\n⋯（報告過長已截斷）"

        return output

    except Exception as e:
        return (
            f"⚠️ 分析 {stock_id} 時發生錯誤：{str(e)}\n"
            f"請稍後再試，或確認代號是否正確。"
        )


def build_scan_reply() -> str:
    """執行每日掃描並回傳報告"""
    try:
        report = daily_stock_scanner()
        if len(report) > 4800:
            report = report[:4800] + "\n\n⋯（報告過長已截斷）"
        return report
    except Exception as e:
        traceback.print_exc()
        return f"⚠️ 掃描時發生錯誤：{str(e)}\n請稍後再試。"


def build_full_market_reply() -> str:
    """執行全市場掃描並回傳報告（這可能需要 10-20 分鐘）"""
    try:
        report = full_market_scanner()
        if len(report) > 4800:
            report = report[:4800] + "\n\n⋯（報告過長已截斷）"
        return report
    except Exception as e:
        traceback.print_exc()
        return f"⚠️ 全市場掃描時發生錯誤：{str(e)}\n請稍後再試。"


# ─── LINE Webhook ──────────────────────────────────────

@app.route("/callback", methods=["POST"])
def callback():
    """LINE Webhook 入口"""
    # 取得 X-Line-Signature 標頭
    signature = request.headers.get("X-Line-Signature", "")

    # 取得請求內容
    body = request.get_data(as_text=True)
    app.logger.info(f"Request body: {body[:200]}...")

    # 處理 webhook body
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    """處理接收到的文字訊息"""
    user_id = event.source.user_id
    text = event.message.text.strip()
    cmd = parse_command(text)

    reply_text = ""

    if cmd["type"] == "help":
        reply_text = build_help_message()

    elif cmd["type"] == "stock":
        stock_id = cmd["stock_id"]
        # 先回覆一則「正在分析」的訊息
        quick_reply = f"🔍 正在分析 {stock_id} {get_stock_name(stock_id)}，請稍候..."
        _reply_message(event, quick_reply)
        # 執行完整分析
        reply_text = build_stock_reply(stock_id)

    elif cmd["type"] == "scan":
        _reply_message(event, "🔍 正在執行每日熱門股池掃描，請稍候（約 1-2 分鐘）...")
        reply_text = build_scan_reply()

    elif cmd["type"] == "full_market":
        _reply_message(
            event,
            "🔍 正在執行全市場掃描（約 10-20 分鐘），完成後會自動回覆報告..."
        )
        # 全市場掃描需要較長時間，用 Thread 非同步處理
        Thread(
            target=_async_full_market_reply,
            args=(event,),
            daemon=True,
        ).start()
        return

    else:
        reply_text = (
            f"❌ 無法識別指令：{text}\n\n"
            f"請輸入「說明」查看使用方式。"
        )

    _reply_message(event, reply_text)


def _reply_message(event, text: str):
    """發送回覆訊息（含錯誤處理）"""
    try:
        with ApiClient(configuration) as api_client:
            line_api = MessagingApi(api_client)
            line_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=text)],
                )
            )
    except Exception as e:
        app.logger.error(f"回覆失敗：{e}")


def _async_full_market_reply(event):
    """非同步執行全市場掃描（在背景執行緒執行）"""
    try:
        report = build_full_market_reply()
        # 全市場掃描完成後無法使用原本的 reply_token（已過期），
        # 所以這裡透過 Messaging API 的 push_message 發送
        with ApiClient(configuration) as api_client:
            line_api = MessagingApi(api_client)
            line_api.push_message(
                to=event.source.user_id,
                messages=[TextMessage(text=report)],
            )
    except Exception as e:
        app.logger.error(f"非同步全市場掃描失敗：{e}")
        try:
            with ApiClient(configuration) as api_client:
                line_api = MessagingApi(api_client)
                line_api.push_message(
                    to=event.source.user_id,
                    messages=[TextMessage(text=f"⚠️ 全市場掃描發生錯誤：{str(e)[:200]}")],
                )
        except Exception:
            pass


# ─── 主程式 ─────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"🤖 台股分析 LINE Bot 啟動中...")
    print(f"   Webhook URL: http://0.0.0.0:{port}/callback")
    print(f"   部署在 Render 時，Webhook URL 為 https://你的服務名稱.onrender.com/callback")
    print(f"   指令說明：輸入「說明」查看使用方式")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    app.run(host="0.0.0.0", port=port, debug=False)
