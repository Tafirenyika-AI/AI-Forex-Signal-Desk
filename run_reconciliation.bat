@echo off
cd /d "C:\Users\tafis\Music\AI FOREX"
echo ---- %date% %time% ---- >> logs\reconciliation.log
".venv\Scripts\python.exe" -m src.scripts.run_reconciliation >> logs\reconciliation.log 2>&1
