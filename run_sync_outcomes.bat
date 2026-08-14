@echo off
cd /d "C:\Users\tafis\Music\AI FOREX"
echo ---- %date% %time% ---- >> logs\sync_outcomes.log
".venv\Scripts\python.exe" -m src.scripts.sync_outcomes >> logs\sync_outcomes.log 2>&1
