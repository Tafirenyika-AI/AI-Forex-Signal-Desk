@echo off
cd /d "C:\Users\tafis\Music\AI FOREX"
echo ---- %date% %time% ---- >> logs\trade_attribution.log
".venv\Scripts\python.exe" -m src.scripts.run_trade_attribution >> logs\trade_attribution.log 2>&1
