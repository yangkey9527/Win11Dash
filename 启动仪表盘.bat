@echo off
rem Win11 桌面悬浮仪表盘 - 双击启动（会弹 UAC 请求管理员权限以读取温度）
cd /d %~dp0
start "" pythonw main.py
