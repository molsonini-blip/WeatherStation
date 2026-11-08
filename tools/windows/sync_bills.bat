@echo off
rem Copies bill PDFs from Google Drive to the ReadySHARE Bills folder that the dashboard reads.
rem Sub-folders (Atmos, TXU, Rhythm Energy, ...) are kept; files already copied are skipped.
set SRC=G:\My Drive\Documents\Personal\Bills
set DST=\\readyshare\Data\WeatherStation\Bills
robocopy "%SRC%" "%DST%" *.pdf /S /XO /R:2 /W:5 /NP /NDL /LOG:"%~dp0sync_bills.log"
if %ERRORLEVEL% GEQ 8 (echo ---- FAILED >> "%~dp0sync_bills.log") else (echo ---- DONE >> "%~dp0sync_bills.log")
