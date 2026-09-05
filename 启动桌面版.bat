@echo off
chcp 65001 >nul
rem ============================================================
rem  ZA量化 桌面版 一键启动
rem  双击本文件即可：自动启动后端 + 打开桌面行情窗口
rem  说明：等价于 cd electron && npm start
rem ============================================================
cd /d "%~dp0electron"
echo [ZA量化] 正在启动桌面版，首次加载请稍候...
echo [ZA量化] 关闭弹出的 ZA量化 窗口即退出程序。
node_modules\.bin\electron.cmd .
pause
