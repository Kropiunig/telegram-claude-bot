@echo off
title Claude Telegram Bot
cd /d "%~dp0"
set "CLAUDE_CMD=%APPDATA%\npm\claude.cmd"
python bot.py
pause
