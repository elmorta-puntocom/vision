@echo off
cd /d "C:\xampp\htdocs\vision\vision\vision_app"
start /B C:\Users\dylan.kiyama.IR3\AppData\Local\Programs\Python\Python311\python.exe app.py
timeout /t 3 /nobreak >nul
start msedge http://127.0.0.1:5050