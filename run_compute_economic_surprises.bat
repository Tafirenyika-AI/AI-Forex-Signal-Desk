@echo off
cd /d "C:\Users\tafis\Music\AI FOREX"
echo ---- %date% %time% ---- >> logs\economic_surprises.log
".venv\Scripts\python.exe" -m src.scripts.compute_economic_surprises >> logs\economic_surprises.log 2>&1
