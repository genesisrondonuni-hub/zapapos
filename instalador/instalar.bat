@echo off
rem ZapaPOS - instalador para Windows 10 (SIN PROBAR en este entorno: ver notas de verificacion).
rem Uso: ejecutar desde la carpeta del paquete (la que tiene app, core, tools, database, fonts, iniciar.py y clave_publica.pem).
setlocal
set "DEST=C:\ZapaPOS"
set "VERSION=1.0.0"
where python >nul 2>&1 || (echo Instale Python 3.12 desde python.org marcando "Add python.exe to PATH" y vuelva a ejecutar. & pause & exit /b 1)
if exist "%DEST%\version_actual.txt" (echo Ya existe una instalacion en %DEST%. Para actualizar use tools.actualizar. & pause & exit /b 1)
python -m pip install --disable-pip-version-check cryptography pillow reportlab || (echo No se pudieron instalar los componentes. & pause & exit /b 1)
mkdir "%DEST%\datos" "%DEST%\versiones\%VERSION%" || exit /b 1
for %%D in (app core tools database fonts) do xcopy /E /I /Y /Q "%%D" "%DEST%\versiones\%VERSION%\%%D" >nul
copy /Y iniciar.py "%DEST%\iniciar.py" >nul
copy /Y clave_publica.pem "%DEST%\clave_publica.pem" >nul
>"%DEST%\version_actual.txt" echo %VERSION%
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\ZapaPOS.lnk'); $s.TargetPath=(Get-Command pythonw).Source; $s.Arguments='\"%DEST%\iniciar.py\"'; $s.WorkingDirectory='%DEST%'; $s.Save()"
echo Instalado en %DEST%. Abra el acceso directo ZapaPOS del escritorio.
pause
