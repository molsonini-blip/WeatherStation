@echo off
rem Pushes the prepared WeatherStation commits to github.com/molsonini-blip/WeatherStation
cd /d %~dp0
echo ==== %date% %time% > push.log
where git >> push.log 2>&1 || goto fail
if exist _push rmdir /s /q _push
git clone -q -b main WeatherStation.bundle _push >> push.log 2>&1 || goto fail
cd _push
git remote set-url origin https://github.com/molsonini-blip/WeatherStation.git >> ..\push.log 2>&1
git push -u origin main >> ..\push.log 2>&1 || goto fail
git log --oneline -3 >> ..\push.log 2>&1
cd ..
rmdir /s /q _push
echo ---- DONE >> push.log
exit /b 0
:fail
echo ---- FAILED >> push.log
exit /b 1
