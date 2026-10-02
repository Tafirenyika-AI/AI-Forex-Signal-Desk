@echo off
cd /d "C:\Users\tafis\Music\AI FOREX"
echo ---- %date% %time% ---- >> logs\sync_sec_edgar.log
".venv\Scripts\python.exe" -m src.scripts.sync_sec_edgar >> logs\sync_sec_edgar.log 2>&1
