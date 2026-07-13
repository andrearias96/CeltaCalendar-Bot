@echo off
chcp 65001 >nul
cd /d "%~dp0"
py main_scraper.py
