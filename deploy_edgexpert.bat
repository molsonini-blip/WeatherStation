@echo off
rem Copies WeatherStation to the EdgeXpert over Tailscale SSH and starts it with Docker.
cd /d %~dp0
set H=molson@100.86.243.19
set O=-o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new
echo ==== %date% %time% > deploy.log
echo ---- check host >> deploy.log
ssh %O% %H% "hostname; uname -m; docker --version; docker compose version; ss -ltnu | grep -E ':8088|:50222' || echo ports-free; ls -d ~/projects" >> deploy.log 2>&1
if errorlevel 1 goto fail
echo ---- copy >> deploy.log
ssh %O% %H% "mkdir -p ~/projects/WeatherStation/data" >> deploy.log 2>&1
scp %O% -r app tools Dockerfile docker-compose.yml requirements.txt README.md .env.example .gitignore tempest-monitor.service .env %H%:projects/WeatherStation/ >> deploy.log 2>&1
if errorlevel 1 goto fail
echo ---- start >> deploy.log
ssh %O% %H% "cd ~/projects/WeatherStation && chmod 600 .env && (docker compose up -d --build 2>&1 || sudo -n docker compose up -d --build 2>&1) && sleep 15 && (docker compose logs --tail 40 2>&1 || sudo -n docker compose logs --tail 40 2>&1) && curl -s -m 5 localhost:8088/api/state | head -c 600" >> deploy.log 2>&1
echo ---- DONE >> deploy.log
exit /b 0
:fail
echo ---- FAILED >> deploy.log
exit /b 1
