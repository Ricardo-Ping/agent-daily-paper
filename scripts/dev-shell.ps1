param(
  [string]$EnvName = 'arxiv-digest-lab'
)

# Force UTF-8 console code page for cleaner Chinese logs
cmd /c chcp 65001 > $null
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$condaBat = Join-Path $env:USERPROFILE 'Anaconda\condabin\conda.bat'
if (-not (Test-Path $condaBat)) {
  $condaBat = 'E:\Anaconda\condabin\conda.bat'
}

if (-not (Test-Path $condaBat)) {
  Write-Error "Cannot find conda.bat. Checked: $condaBat"
  exit 1
}

Write-Host "[dev-shell] UTF-8 enabled (chcp 65001)" -ForegroundColor Green
Write-Host "[dev-shell] Activating conda env: $EnvName" -ForegroundColor Green

$activateCmd = "`"$condaBat`" activate $EnvName"
cmd /k $activateCmd
