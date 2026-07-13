@echo off
set "DEVECO_HVIGORW=E:\DevEcoStudio\DevEco Studio\tools\hvigor\bin\hvigorw.bat"

if not exist "%DEVECO_HVIGORW%" (
  echo ERROR: DevEco hvigorw.bat not found: %DEVECO_HVIGORW%
  exit /b 1
)

call "%DEVECO_HVIGORW%" %*
