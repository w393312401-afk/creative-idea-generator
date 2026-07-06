@echo off
chcp 65001 >nul
cd /d "%~dp0"
title SPARK 停止服务

echo [SPARK] 正在停止 8085 主服务及所有残留的 8086 旧实例...
rem 服务入口固定为 8085；8086 是历史图像子服务端口，只做清理不再启动。
rem 用 PowerShell 一次清掉两个端口上的全部监听进程，避免旧脚本"只杀第一个"导致实例堆积。
powershell -NoProfile -Command "Get-NetTCPConnection -State Listen -LocalPort 8085,8086 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }"
if exist server.pid del server.pid >nul 2>&1
echo [SPARK] 已停止。
timeout /t 2 >nul
