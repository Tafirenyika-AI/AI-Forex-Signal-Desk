@echo off
cd /d "C:\Users\tafis\Music\AI FOREX"
echo ---- %date% %time% ---- >> logs\ingest_knowledge.log
".venv\Scripts\python.exe" -m src.scripts.ingest_knowledge >> logs\ingest_knowledge.log 2>&1
