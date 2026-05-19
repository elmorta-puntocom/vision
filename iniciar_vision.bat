@echo off
cd /d "C:\xampp\htdocs\vision\vision\vision_app"
call "C:\xampp\htdocs\vision\vision\.venv\Scripts\activate.bat"
start /B python app.py
timeout /t 3 /nobreak >nul
start msedge http://127.0.0.1:5050