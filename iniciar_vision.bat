@echo off
title Servidor de Vision Artificial

cd /d "C:\Users\juanc\vision\vision"

start "" ".\.venv\Scripts\python.exe" run.py

timeout /t 3 /nobreak >nul

start msedge "http://127.0.0.1:5050"

exit