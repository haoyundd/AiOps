@echo off
setlocal
cd /d "%~dp0"
if "%MERCHANTFLOW_DIR%"=="" set "MERCHANTFLOW_DIR=%~dp0..\MerchantFlow-Pro"

if exist "%MERCHANTFLOW_DIR%\docker-compose.yml" (
  pushd "%MERCHANTFLOW_DIR%"
  docker compose down
  popd
)
docker compose -f docker-compose.incident.yml down
