@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
title Radar - Instalador
cd /d "%~dp0"
echo.
echo  ============================================
echo     RADAR - eventos naturais e humanos em PT
echo     Instalador das bibliotecas
echo  ============================================
echo.

rem ---------- 1) Python ----------
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY where py >nul 2>&1 && set "PY=py -3"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY (
  echo  [1/4] O Python nao esta instalado. Vou instala-lo com o winget.
  echo        O Windows vai mostrar os termos do Python: le e aceita tu, se concordares.
  echo.
  winget install -e --id Python.Python.3.12
  if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)
if not defined PY (
  echo.
  echo  Nao consegui instalar o Python automaticamente.
  echo  Instala-o a partir de https://www.python.org/downloads/ ^(marca "Add python.exe to PATH"^)
  echo  e volta a abrir este instalador.
  pause
  exit /b 1
)
echo  [1/4] Python encontrado:
%PY% --version

rem ---------- 2) bibliotecas ----------
echo.
echo  [2/4] A instalar as bibliotecas (pywebview para a janela da app)...
%PY% -m pip install --upgrade pip --quiet
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo  Houve um erro a instalar as bibliotecas. Verifica a ligacao a internet e tenta outra vez.
  pause
  exit /b 1
)

rem ---------- 3) atalhos (menu Iniciar + ambiente de trabalho) ----------
echo.
echo  [3/4] A criar os atalhos com o icone do Radar...
set "PYW="
for /f "usebackq delims=" %%P in (`%PY% -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"`) do set "PYW=%%P"
if not exist "%PYW%" set "PYW=pythonw"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s=New-Object -ComObject WScript.Shell; $d=(Get-Location).Path;" ^
  "foreach($dir in @([Environment]::GetFolderPath('Programs'),[Environment]::GetFolderPath('Desktop'))){" ^
  " $l=$s.CreateShortcut((Join-Path $dir 'Radar.lnk')); $l.TargetPath='%PYW%'; $l.Arguments='app.py'; $l.WorkingDirectory=$d; $l.IconLocation=(Join-Path $d 'icon.ico')+',0'; $l.Description='Radar: eventos em tempo real'; $l.Save() };" ^
  "$l=$s.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Programs')) 'Radar - Comando.lnk')); $l.TargetPath='%PYW%'; $l.Arguments='app.py --comando'; $l.WorkingDirectory=$d; $l.IconLocation=(Join-Path $d 'icon.ico')+',0'; $l.Save()"

rem ---------- 4) chave da IA (opcional) ----------
echo.
echo  [4/4] Pesquisa e resumos com IA (opcional)
echo        Com uma chave gratuita da Groq (https://console.groq.com/keys) o Radar
echo        resume as guerras, reconhece ataques e pesquisa em linguagem natural.
echo        Sem chave, tudo o resto funciona na mesma.
if exist groq_key.txt (
  echo        Ja existe uma chave em groq_key.txt.
) else (
  set "KEY="
  set /p "KEY=       Cola aqui a tua chave (ou carrega Enter para saltar): "
  if defined KEY (
    > groq_key.txt echo !KEY!
    echo        Chave guardada em groq_key.txt
  )
)

echo.
echo  ============================================
echo   Pronto. Abre o Radar pelo atalho "Radar"
echo   (menu Iniciar ou ambiente de trabalho).
echo  ============================================
echo.
set /p "ABRIR=  Abrir o Radar agora? (S/N) "
if /i "%ABRIR%"=="S" start "" "%PYW%" app.py
exit /b 0
