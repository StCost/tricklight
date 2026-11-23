# TrickLight tray uninstaller. Run:  powershell -ExecutionPolicy Bypass -File uninstall.ps1
$ErrorActionPreference = 'Continue'
$lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'TrickLight.lnk'
if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Host "Removed $lnk" } else { Write-Host 'No startup shortcut found' }
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -like '*tricklight_tray*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host "Stopped PID $($_.ProcessId)" }
Write-Host 'Done. (Python packages and config/log files were left in place.)'
