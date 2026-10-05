@echo off
rem ===================================================================
rem  DouyinChatDumpTool-Android launcher (Windows)
rem
rem  NOTE 1: this file is intentionally ASCII-only in comments AND in
rem          its own output strings. cmd.exe decodes a .bat using the
rem          code page active when it opens the file, so UTF-8 Chinese
rem          inside a .bat can be mis-decoded into stray & | ( ) bytes,
rem          splitting one line into several bogus commands. All Chinese
rem          UI text is printed by dydump_android.py, which writes to the
rem          Windows console through the wide-char API and is therefore
rem          encoding-safe.
rem  NOTE 2: no setlocal enabledelayedexpansion here -- it would eat the
rem          "!" in messages like "[!] ...". Branching uses goto +
rem          errorlevel so that %ans% expands correctly after set /p.
rem  NOTE 3: the launcher stays quiet when everything is fine; it only
rem          speaks up about problems, then hands over to the menu.
rem ===================================================================

chcp 65001 >nul
cd /d "%~dp0"
title DouyinChatDumpTool-Android

rem ---------- 1. locate Python ----------
set "PY="
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
    python -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY goto no_python

rem ---------- 2. check dependencies ----------
%PY% -c "import Crypto" >nul 2>nul
if not errorlevel 1 goto deps_ok

echo.
echo   [!] Python dependencies are missing (pycryptodome).
echo       A one-time online install is needed.
echo.
set "ans="
set /p "ans=      Install now? [Y/n] "
if /i "%ans%"=="n" goto deps_ok
echo.
%PY% -m pip install -r requirements.txt
if not errorlevel 1 goto deps_verify
echo.
echo   [!] Direct install failed, retrying with a China mirror ...
%PY% -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

:deps_verify
%PY% -c "import Crypto" >nul 2>nul
if not errorlevel 1 goto deps_installed
echo.
echo   [!] Still not installed. Run this to see the real error:
echo       %PY% -m pip install -r requirements.txt
echo.
echo       If the system Python refuses, use a virtual env:
echo       %PY% -m venv .venv
echo       .venv\Scripts\python.exe -m pip install -r requirements.txt
echo       then replace python in this file with .venv\Scripts\python.exe
echo.
pause
exit /b 1

:deps_installed
echo   [*] Dependencies installed.

:deps_ok
rem ---------- 3. check adb (warning only; not needed for local data) ----------
set "ADBOK="
where adb >nul 2>nul
if not errorlevel 1 set "ADBOK=1"
if exist "%~dp0platform-tools\adb.exe" set "ADBOK=1"
if defined ADBOK goto run

echo.
echo   [!] adb (platform-tools) not found.
echo       Ignore this if you only process data already on disk.
echo       To pull from the phone, either:
echo         - pick option 7) in the menu below to download adb, or
echo         - download it manually from
echo           https://developer.android.com/tools/releases/platform-tools
echo           and unzip so that platform-tools\adb.exe sits next to this file.
echo.

:run
%PY% dydump_android.py %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo   [!] exit code %RC%
pause
exit /b %RC%

:no_python
echo.
echo   [!] Python 3 not found.
echo.
echo       Install 3.8+ from https://www.python.org/downloads/
echo       On the first installer screen, tick "Add python.exe to PATH",
echo       then reopen this window.
echo.
pause
exit /b 1
