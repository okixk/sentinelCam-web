@echo off

rem 1. Check if sentinelCam-worker is already installed
if exist ".sentinelCam-installed" (
    echo sentinelCam-worker bereits installiert.
    goto :end
)

rem 2. Ask if dependencies should be installed
echo sentinelCam-worker nicht gefunden.
set /p INSTALL_DEPS="Moechtest du die Dependencies installieren? (j/n): "
if /i not "%INSTALL_DEPS%"=="j" goto :skip

rem 3. Clone repository into temp folder
echo Klone Repository...
git clone https://github.com/okixk/sentinelCam-worker.git _temp_clone
if errorlevel 1 (
    echo Fehler beim Klonen des Repositories. Ist git installiert?
    exit /b 1
)

rem 4. Move all files from temp folder to current directory
xcopy "_temp_clone\*" "." /e /h /y /q

rem 5. Delete temp folder (remove read-only flags first)
attrib -r -h -s "_temp_clone\*.*" /s /d
rd /s /q "_temp_clone"

rem 6. Set permissions for current user
attrib -r -h -s "*.*" /s /d
icacls "." /reset /t /q
icacls "." /setowner "%USERNAME%" /t /q
icacls "." /grant:r "%USERNAME%":(OI)(CI)F /t /q

rem 7. Create marker file
echo installed> ".sentinelCam-installed"

echo Dependencies erfolgreich installiert.

rem 8. Start run.bat
echo Starte run.bat...
call run.bat
goto :end

:skip
echo Installation uebersprungen.

:end
