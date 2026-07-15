# 讓 Render 服務保持清醒（24小時不睡著）

Render 免費方案會在 15 分鐘無人使用後休眠。
使用 UptimeRobot 定時 ping 服務，即可避免休眠。

## 設定步驟

1. 開啟 https://uptimerobot.com 註冊免費帳號

2. 登入後點 **Add New Monitor**

3. 填寫：
   - Monitor Type：**HTTP(s)**
   - Friendly Name：`tw-stock-bot`
   - URL (or IP)：`https://tw-stock-bot.onrender.com/callback`
   - Monitoring Interval：**5 minutes**
   - 其他維持預設

4. 按 **Create Monitor**

5. 完成後 UptimeRobot 會每 5 分鐘 ping 一次你的服務，
   Render 就不會進入休眠，LINE Bot 反應就會變快。

## 加速分析的其他建議

在 Render 設定中也可以調整 `BACKTEST_PERIOD`（預設 5 年），
修改 `stock_bot.py` 第 18 行的 `BACKTEST_PERIOD = "5y"` 為 `"2y"` 可加快資料下載速度，

但目前先裝 UptimeRobot 就好，應該就能解決反應慢的問題。