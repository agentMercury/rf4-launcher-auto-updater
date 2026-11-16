$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$ico  = Join-Path $here 'cf4.ico'
$gui  = Join-Path $here 'rf4_updater_gui.py'
if (-not (Test-Path $ico)) { throw ('icon not found: ' + $ico) }
if (-not (Test-Path $gui)) { throw ('gui not found: ' + $gui) }
# Prefer a real interpreter; the WindowsApps pythonw.exe is a Store alias stub and is not reliable.
$pyw = $null
$cands = @(
  (Join-Path $env:LOCALAPPDATA 'Python\pythoncore-3.15-64\pythonw.exe'),
  (Join-Path $env:LOCALAPPDATA 'Python\bin\pythonw.exe'),
  'C:\Python313\pythonw.exe',
  'C:\Python312\pythonw.exe'
)
foreach ($c in $cands) { if ((-not $pyw) -and (Test-Path $c)) { $pyw = $c } }
if (-not $pyw) {
  $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
  if ($cmd -and ($cmd.Source -notlike '*\WindowsApps\*')) { $pyw = $cmd.Source }
}
if (-not $pyw) { throw 'pythonw.exe not found (install Python 3 with tkinter, or add it to PATH)' }
$desk = [Environment]::GetFolderPath('Desktop')
foreach ($old in @('RF4登录器更新.lnk','RF4 Launcher Update.lnk')) {
  $p = Join-Path $desk $old
  if (Test-Path $p) { Remove-Item -LiteralPath $p -Force }
}
$lnk = Join-Path $desk 'Chinese Fishing 4.lnk'
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($lnk)
$s.TargetPath = $pyw
$s.Arguments = '"' + $gui + '"'
$s.WorkingDirectory = $here
$s.IconLocation = $ico + ',0'
$s.Description = 'Chinese Fishing 4 - 登录器自动更新'
$s.Save()
'shortcut: ' + $lnk
'target  : ' + $pyw
'icon    : ' + $s.IconLocation
