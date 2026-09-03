@echo off
cd /d "%~dp0"
python amz_arama.py
if errorlevel 1 pause
