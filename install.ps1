# TrickLight tray installer. Run:  powershell -ExecutionPolicy Bypass -File install.ps1
$ErrorActionPreference = 'Stop'
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $dir 'tricklight_tray.pyw'
if (!(Test-Path $script)) { throw "Missing $script" }

Write-Host 'Installing Python deps (py -3.12)...'
& py -3.12 -m pip install --upgrade bleak pystray pillow
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }

$pyw = (& py -3.12 -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))").Trim()
if (!(Test-Path $pyw)) { throw "pythonw.exe not found at $pyw" }

# stop a running copy (so the new one takes over)
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -like '*tricklight_tray*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

$lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'TrickLight.lnk'
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($lnk)
$s.TargetPath = $pyw
$s.Arguments = "`"$script`""
$s.WorkingDirectory = $dir
$s.Description = 'TrickLight tray (BLE light control)'
$s.Save()
Write-Host "Startup shortcut: $lnk"

Start-Process -FilePath $pyw -ArgumentList "`"$script`"" -WorkingDirectory $dir
Write-Host 'TrickLight started (see tray). Log:' (Join-Path $dir 'tricklight.log')
