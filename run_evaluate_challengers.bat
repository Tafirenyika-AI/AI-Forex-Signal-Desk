@echo off
cd /d "C:\Users\tafis\Music\AI FOREX"
echo ---- %date% %time% ---- >> logs\evaluate_challengers.log
".venv\Scripts\python.exe" -m src.scripts.evaluate_challengers >> logs\evaluate_challengers.log 2>&1
