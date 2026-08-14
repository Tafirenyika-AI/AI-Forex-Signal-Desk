@echo off
cd /d "C:\Users\tafis\Music\AI FOREX"
echo ---- %date% %time% ---- >> logs\promotion_gates.log
".venv\Scripts\python.exe" -m src.scripts.snapshot_promotion_gates >> logs\promotion_gates.log 2>&1
