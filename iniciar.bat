@echo off
title Agente Mesa de Ayuda - SOMOS NETWORK
cd /d "%~dp0"

REM Verificar si py funciona
py --version >nul 2>&1
if %errorlevel% equ 0 (
    py main.py
    goto fin
)

REM Si no, usar la ruta directa de pythoncore
if exist "%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe" (
    "%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe" main.py
    goto fin
)

echo No se encontro Python. Por favor verifica la instalacion.
pause

:fin
