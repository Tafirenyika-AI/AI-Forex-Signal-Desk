@echo off
cd /d "C:\Users\tafis\Music\AI FOREX"
echo ---- %date% %time% ---- >> logs\calendar_ingest.log
".venv\Scripts\python.exe" -m src.scripts.ingest_calendar >> logs\calendar_ingest.log 2>&1
