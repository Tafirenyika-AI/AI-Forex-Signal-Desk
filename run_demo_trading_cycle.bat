@echo off
cd /d "C:\Users\tafis\Music\AI FOREX"
echo ---- %date% %time% ---- >> logs\demo_trading.log
".venv\Scripts\python.exe" -m src.run_loop --mode demo --once --auto-execute >> logs\demo_trading.log 2>&1
