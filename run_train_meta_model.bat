@echo off
cd /d "C:\Users\tafis\Music\AI FOREX"
echo ---- %date% %time% ---- >> logs\train_meta_model.log
".venv\Scripts\python.exe" -m src.models.train_meta_model >> logs\train_meta_model.log 2>&1
