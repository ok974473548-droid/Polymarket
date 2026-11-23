@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto try_python
py -3 -c "import sys; sys.exit(sys.version_info < (3,11))" >nul 2>nul
if errorlevel 1 goto try_python
start "" powershell -NoProfile -Command "for ($i=0; $i -lt 30; $i++) {try {Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8787/api/health -TimeoutSec 1 -ErrorAction Stop | Out-Null; Start-Process http://127.0.0.1:8787; break} catch {Start-Sleep -Seconds 1}}"
py -3 app.py
goto end
:try_python
where python >nul 2>nul
if errorlevel 1 goto try_docker
python -c "import sys; sys.exit(sys.version_info < (3,11))" >nul 2>nul
if errorlevel 1 goto try_docker
start "" powershell -NoProfile -Command "for ($i=0; $i -lt 30; $i++) {try {Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8787/api/health -TimeoutSec 1 -ErrorAction Stop | Out-Null; Start-Process http://127.0.0.1:8787; break} catch {Start-Sleep -Seconds 1}}"
python app.py
goto end
:try_docker
docker compose up -d --build --wait
if errorlevel 1 goto failed
start "" http://127.0.0.1:8787
exit /b 0
:failed
echo Install Python 3.11+ or start Docker Desktop and retry.
:end
pause
