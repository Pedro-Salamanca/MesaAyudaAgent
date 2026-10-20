@echo off
echo ===================================================
echo   Compilador del Agente Mesa de Ayuda a .EXE (v3.0)
echo ===================================================
echo Instalando PyInstaller (por si no lo tienes)...
pip install pyinstaller

echo.
echo Compilando el agente en un solo archivo...
echo (Esto puede tardar un par de minutos, por favor espera)
echo.

py -m PyInstaller --noconfirm --onefile --windowed --name "AgenteMesaAyuda" --add-data "core;core" --add-data "ui;ui" --collect-all customtkinter --collect-all google.genai --collect-all pydantic main.py

echo.
echo ===================================================
echo   COMPILACION FINALIZADA
echo ===================================================
echo.
echo Tu archivo ejecutable (.exe) se encuentra en la carpeta:
echo C:\Users\USUARIO\Downloads\PROGRAMADOR\MesaAyudaAgent\dist\
echo.
pause
