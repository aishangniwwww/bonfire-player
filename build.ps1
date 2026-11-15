param([string]$Out = "dist")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = (Get-Command python).Source

& $py -m PyInstaller --noconfirm --onefile --noconsole `
  --name "Bonfire" `
  --icon (Join-Path $root "app.ico") `
  --add-data "$(Join-Path $root 'index.html');web" `
  --distpath (Join-Path $root $Out) `
  --workpath (Join-Path $root "_build") `
  --specpath (Join-Path $root "_build") `
  (Join-Path $root "app.py")

Write-Host ""
Write-Host ("完成：" + (Join-Path $root "$Out\Bonfire.exe"))
