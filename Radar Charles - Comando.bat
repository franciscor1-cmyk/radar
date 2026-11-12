@echo off
cd /d "%~dp0"
python -m pip show pywebview >nul 2>&1 || python -m pip install pywebview
python app.py --comando
