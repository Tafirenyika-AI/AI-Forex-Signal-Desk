@echo off
cd /d "C:\Users\tafis\Music\AI FOREX"
echo ---- %date% %time% ---- >> logs\central_bank.log
".venv\Scripts\python.exe" -m src.scripts.ingest_central_bank >> logs\central_bank.log 2>&1
