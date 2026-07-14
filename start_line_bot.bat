@echo off
chcp 65001 >nul
title 台股分析 LINE Bot

echo ╔══════════════════════════════════════╗
echo ║   台股分析 LINE Bot 啟動程式        ║
echo ╚══════════════════════════════════════╝
echo.

:: 檢查是否安裝 ngrok
where ngrok >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [提醒] 未偵測到 ngrok
    echo 請至 https://ngrok.com/download 下載安裝
    echo 或手動部署至正式環境
    echo.
    set USE_NGROK=0
) else (
    set USE_NGROK=1
)

:: 啟動 LINE Bot
echo [1/2] 啟動 LINE Bot 伺服器...
echo 請注意：如果這是第一次執行，可能需要等待 LINE Bot 初始化
echo.
start "LINE Bot" cmd /c "python line_bot.py"

:: 等待 Flask 啟動
timeout /t 3 /nobreak >nul

:: 啟動 ngrok（如果有安裝）
if "%USE_NGROK%"=="1" (
    echo [2/2] 啟動 ngrok 隧道...
    start "ngrok" cmd /c "ngrok http 5000 --log=stdout"
    echo.
    echo ngrok 已啟動，請開啟 http://localhost:4040 查看公開網址
)

echo.
echo 服務啟動完成！
echo.
if "%USE_NGROK%"=="1" (
    echo LINE Webhook URL 設定方式：
    echo 1. 開啟 https://developers.line.biz/console/
    echo 2. 進入你的 Messaging API Channel
    echo 3. 在 Webhook 設定中填入：
    echo    https://你的ngrok網址/callback
    echo 4. 記得開啟「Use webhook」
) else (
    echo ngrok 未安裝，請手動部署：
    echo 1. 部署至正式環境（Render / Heroku / VPS）
    echo 2. 或安裝 ngrok 後重新執行此腳本
)
echo.
pause