@echo off
setlocal EnableExtensions
rem MusicPad build + download of the official VB-CABLE driver package.
rem VB-CABLE is downloaded from the official VB-Audio website and is NOT
rem silently installed on the build PC. The user installs it on the target PC.

cd /d "%~dp0"

set "VB_URL=https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip"
set "VB_ZIP=%TEMP%\VBCABLE_Driver_Pack45.zip"
set "VB_DIR=%~dp0dist\VB-CABLE"

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.10-3.13 and enable Add Python to PATH.
  goto fail
)

echo [1/4] Installing Python dependencies...
python -m pip install --upgrade pip
if errorlevel 1 goto fail
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto fail

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist MusicPad.spec del /q MusicPad.spec

echo [2/4] Building MusicPad.exe...
python -m PyInstaller --clean --noconfirm --noconsole --onefile --name MusicPad ^
  --collect-all sounddevice --collect-all _sounddevice_data ^
  --collect-all proctap --collect-all pycaw --collect-all comtypes ^
  --hidden-import pystray._win32 ^
  musicpad_app.py
if errorlevel 1 goto fail

 echo [3/4] Downloading official VB-CABLE driver package...
if not exist "%VB_DIR%" mkdir "%VB_DIR%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; Invoke-WebRequest -Uri '%VB_URL%' -OutFile '%VB_ZIP%'; Expand-Archive -LiteralPath '%VB_ZIP%' -DestinationPath '%VB_DIR%' -Force"
if errorlevel 1 (
  echo Could not download or extract VB-CABLE. Check your internet connection.
  goto fail
)
set "VB_SETUP_FOUND="
for /r "%VB_DIR%" %%F in (VBCABLE_Setup_x64.exe) do set "VB_SETUP_FOUND=1"
if not defined VB_SETUP_FOUND for /r "%VB_DIR%" %%F in (VBCABLE_Setup.exe) do set "VB_SETUP_FOUND=1"
if not defined VB_SETUP_FOUND (
  echo VB-CABLE setup executable was not found in the downloaded archive.
  echo Check the package contents in: %VB_DIR%
  goto fail
)

 echo [4/4] Copying the VB-CABLE installer helper...
copy /y "%~dp0Install_VB-CABLE.bat" "%~dp0dist\Install_VB-CABLE.bat" >nul
if errorlevel 1 goto fail

if exist "%VB_ZIP%" del /q "%VB_ZIP%"
echo.
echo Build completed:
echo   dist\MusicPad.exe
echo   dist\Install_VB-CABLE.bat
echo   dist\VB-CABLE\ (official driver files)
echo.
echo On each target PC, run Install_VB-CABLE.bat as administrator,
echo complete the VB-CABLE installer, then restart Windows.
pause
exit /b 0

:fail
echo.
echo Build failed. See the messages above.
pause
exit /b 1
