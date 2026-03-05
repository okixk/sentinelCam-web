@echo off

rem 1. Check if all required files are already present
set ALL_PRESENT=1
for %%F in (webcam.py webcam.properties run.sh run.bat) do (
    if not exist "%%F" set ALL_PRESENT=0
)
if "%ALL_PRESENT%"=="1" (
    echo sentinelCam-worker bereits installiert.
    goto :end
)

rem 2. Ask if dependencies should be installed
echo sentinelCam-worker nicht gefunden.
set /p INSTALL_DEPS="Möchtest du die Dependencies installieren? (j/n): "
if /i not "%INSTALL_DEPS%"=="j" goto :skip

rem 3. Clone repository into temp folder
echo Klone Repository...
git clone https://github.com/okixk/sentinelCam-worker.git _temp_clone
if errorlevel 1 (
    echo Fehler beim Klonen des Repositories. Ist git installiert?
    exit /b 1
)

rem 4. Copy only required files from temp folder
for %%F in (webcam.py webcam.properties run.sh run.bat) do (
    if exist "_temp_clone\%%F" (
        copy /y "_temp_clone\%%F" ".\%%F" >nul
    ) else (
        echo Warnung: %%F nicht im Repository gefunden.
    )
)

rem 5. Delete temp folder (remove read-only flags first)
attrib -r -h -s "_temp_clone\*.*" /s /d
rd /s /q "_temp_clone"

rem 6. Set permissions for copied files
for %%F in (webcam.py webcam.properties run.sh run.bat) do (
    if exist "%%F" (
        icacls "%%F" /grant:r "%USERNAME%":F /q >nul
    )
)

echo Dependencies erfolgreich installiert.

rem 8. Start run.bat
echo Starte run.bat...
call run.bat --web --stream webrtc --port 8080 --webrtc-codec auto
goto :end

:skip
echo Installation Übersprungen.

:end
