@echo off
chcp 65001 >nul
REM 本文件位于仓库的 assistant/ 目录；%~dp0 会自己定位，从哪调用都行。
cd /d %~dp0
REM 数据（对话历史 / api_key.json）默认落在本目录，和平台自动拉起时是同一份。
REM 想换个位置就取消下面这行的注释并改成目标路径：
REM set MYWHEEL_DATA_DIR=%~dp0data

set MYWHEEL_PORT=8001

if "%MYWHEEL_API_KEY%"=="" (
  echo [提示] 没设置 MYWHEEL_API_KEY。
  echo        方式一：本窗口执行  set MYWHEEL_API_KEY=sk-...
  echo        方式二：启动后在助手页面的设置里填 Key
  echo        方式三：在 codingstorm.toml 的 [assistant] api_key 里填，
  echo                或留空让它复用 [claude.env] 的 ANTHROPIC_AUTH_TOKEN
  echo.
)

python server.py
pause
