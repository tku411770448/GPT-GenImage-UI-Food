@echo off
setlocal
REM Windows setup for OpenAI GPT Image API defect editing.
REM Run this in cmd from the project root.

py -3.10 -m venv .venv
if errorlevel 1 (
  echo [WARN] Python 3.10 launcher not found. Trying default python...
  python -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Could not create .venv. Install Python 3.10/3.11 first.
    exit /b 1
  )
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
python scripts\verify_env.py
