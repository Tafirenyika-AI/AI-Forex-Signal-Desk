@echo off
cd /d "C:\Users\tafis\Music\AI FOREX"
echo ---- %date% %time% ---- >> logs\gdelt_news.log
".venv\Scripts\python.exe" -m src.scripts.ingest_gdelt_news >> logs\gdelt_news.log 2>&1
