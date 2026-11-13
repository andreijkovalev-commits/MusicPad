@echo off
setlocal EnableExtensions
rem Installs the official VB-CABLE driver included in the adjacent VB-CABLE folder.

net session >nul 2>&1
if not "%errorlevel%"=="0" (
  echo Administrator permission is required to install an audio driver.
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

set "DRIVER_DIR=%~dp0VB-CABLE"
if not exist "%DRIVER_DIR%" (
  echo Driver folder not found: %DRIVER_DIR%
  echo Run build.bat first and keep the VB-CABLE folder next to this file.
  pause
  exit /b 1
)

set "SETUP="
for /r "%DRIVER_DIR%" %%F in (VBCABLE_Setup_x64.exe) do if not defined SETUP set "SETUP=%%F"
if not defined SETUP for /r "%DRIVER_DIR%" %%F in (VBCABLE_Setup.exe) do if not defined SETUP set "SETUP=%%F"

if not defined SETUP (
  echo VB-CABLE setup program was not found in %DRIVER_DIR%.
  pause
  exit /b 1
)

echo Starting VB-CABLE driver setup:
echo %SETUP%
echo.
echo Follow the installer instructions and click Install Driver.
start "VB-CABLE Setup" /wait "%SETUP%"

echo.
echo VB-CABLE setup has closed.
echo Restart Windows before using the new audio devices.
echo In MusicPad select CABLE Input as the virtual output device.
echo In Discord select CABLE Output as the input microphone.
pause
