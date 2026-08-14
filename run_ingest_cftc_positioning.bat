@echo off
cd /d "C:\Users\tafis\Music\AI FOREX"
echo ---- %date% %time% ---- >> logs\cftc_positioning.log
".venv\Scripts\python.exe" -m src.scripts.ingest_cftc_positioning >> logs\cftc_positioning.log 2>&1
