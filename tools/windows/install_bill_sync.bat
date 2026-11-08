@echo off
rem Runs sync_bills.bat every hour (while you're signed in, so Google Drive is available), then once right now.
cd /d %~dp0
schtasks /Create /F /SC HOURLY /TN "WeatherStation Bill Sync" /TR "\"%~dp0sync_bills.bat\"" > install_bill_sync.log 2>&1
call "%~dp0sync_bills.bat"
type install_bill_sync.log
