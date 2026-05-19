@echo off
cd /d "C:\xampp\htdocs\vision\vision"
call "C:\xampp\htdocs\vision\vision\.venv\Scripts\activate.bat"
start /B python run.py
timeout /t 3 /nobreak >nul
start msedge http://127.0.0.1:5050
