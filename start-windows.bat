@echo off
setlocal
cd /d "%~dp0"

if not exist ".env" (
  echo Missing haoyundd\.env. Copy .env.example and set unique passwords first.
  exit /b 1
)

if "%MERCHANTFLOW_DIR%"=="" set "MERCHANTFLOW_DIR=%~dp0..\MerchantFlow-Pro"
if not exist "%MERCHANTFLOW_DIR%\docker-compose.yml" (
  echo MerchantFlow-Pro was not found at "%MERCHANTFLOW_DIR%".
  exit /b 1
)
if not exist "%MERCHANTFLOW_DIR%\.env" (
  echo Missing MerchantFlow-Pro\.env. Copy its .env.example and set secrets first.
  exit /b 1
)

docker compose -f docker-compose.incident.yml up -d --build
if errorlevel 1 exit /b 1

pushd "%MERCHANTFLOW_DIR%"
docker compose up -d --build
set "RESULT=%ERRORLEVEL%"
popd
exit /b %RESULT%
