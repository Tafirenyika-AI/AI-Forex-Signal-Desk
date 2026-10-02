@echo off
cd /d "C:\Users\tafis\Music\AI FOREX"
echo ---- %date% %time% ---- >> logs\sync_equity_news.log
".venv\Scripts\python.exe" -m src.scripts.sync_equity_news >> logs\sync_equity_news.log 2>&1
