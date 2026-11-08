@echo off
rem Copies bill and property PDFs from Google Drive to ReadySHARE, where the dashboard reads them.
rem Runs hourly (install_bill_sync.bat). Sub-folders are kept; files already copied are skipped.
rem Only the folders listed here are copied - nothing else from the Property folder (tax returns, IDs, contracts) leaves Drive.
set LOG=%~dp0sync_bills.log
set O=/XO /R:2 /W:5 /NP /NDL
echo ==== %date% %time% > "%LOG%"

rem ---- home bills (Bills tab)
set SRC=G:\My Drive\Documents\Personal\Bills
set DST=\\readyshare\Data\WeatherStation\Bills
robocopy "%SRC%" "%DST%" *.pdf /S %O% /LOG+:"%LOG%"

rem ---- properties (Properties tab): new statements go in Property\Dashboard\<property name>
set P=G:\My Drive\Documents\Personal\Property
set PD=\\readyshare\Data\WeatherStation\Properties
robocopy "%P%\Dashboard" "%PD%" *.pdf /S %O% /LOG+:"%LOG%"
rem statements already filed elsewhere in the Property folder
robocopy "%P%\16609 Malaga Hills DR\Morgage" "%PD%\16609 Malaga Hills Dr\Mortgage" *.pdf /S %O% /LOG+:"%LOG%"
robocopy "%P%\9111 United Kingdom\Morgtage" "%PD%\9111 United Kingdom Dr\Mortgage" *.pdf /S %O% /LOG+:"%LOG%"
robocopy "%P%\16609 Malaga Hills DR\UWM\Statements" "%PD%\16609 Malaga Hills Dr\UWM" *.pdf %O% /LOG+:"%LOG%"
robocopy "%P%\16609 Malaga Hills DR\HELOC - CACU" "%PD%\_Statements Sept 2026" "*Statment.pdf" "*Statement.pdf" %O% /LOG+:"%LOG%"
robocopy "%P%\Refinance" "%PD%\_Statements Aug 2023" "*statment.pdf" "*statement.pdf" %O% /LOG+:"%LOG%"
robocopy "%P%\2403 Wilma Rudolph\Mortgage\Truist" "%PD%\2403 Wilma Rudolph Rd\Truist" *.pdf /S %O% /LOG+:"%LOG%"
robocopy "%P%\11528 Rustic Rock\Statements" "%PD%\11528 Rustic Rock Dr\Statements" *.pdf /S %O% /LOG+:"%LOG%"
robocopy "%P%\11528 Rustic Rock\Utilities" "%PD%\11528 Rustic Rock Dr\Utilities" *.pdf /S %O% /LOG+:"%LOG%"
rem Presidio Group owner statements (rent and expenses); the refinancing folder (bank statements, paystubs) is left out
robocopy "%P%\Presidio Group" "%PD%\_Presidio Group" *.pdf /S /XD "Refinancing 16609 Malaga Hills" %O% /LOG+:"%LOG%"
echo ---- DONE >> "%LOG%"
