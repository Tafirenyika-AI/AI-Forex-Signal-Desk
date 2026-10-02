@echo off
cd /d "C:\Users\tafis\Music\AI FOREX"
echo ---- %date% %time% ---- >> logs\sync_equity_relationships.log
".venv\Scripts\python.exe" -m src.scripts.sync_equity_relationships >> logs\sync_equity_relationships.log 2>&1
