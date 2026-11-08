@echo off
rem Pushes the latest commits to GitHub, then redeploys to the EdgeXpert.
cd /d %~dp0
call push_github.bat
call deploy_edgexpert.bat
