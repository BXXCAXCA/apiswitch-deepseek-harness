[CmdletBinding()]
param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$pluginRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$manifest = Get-Content -Raw -LiteralPath (Join-Path $pluginRoot ".codex-plugin\plugin.json") | ConvertFrom-Json
$distRoot = Join-Path $pluginRoot "dist"
$stageRoot = Join-Path $distRoot $manifest.name
$archivePath = Join-Path $distRoot "APISwitch-DeepSeek-Harness-v$($manifest.version).zip"
$frontendIndex = Join-Path $pluginRoot "frontend\dist\index.html"

if (-not (Test-Path -LiteralPath $frontendIndex)) {
    throw "frontend/dist is missing. Run npm ci and npm run build in frontend first."
}
if ($Clean -and (Test-Path -LiteralPath $stageRoot)) {
    Remove-Item -LiteralPath $stageRoot -Recurse -Force
}
if (Test-Path -LiteralPath $archivePath) { Remove-Item -LiteralPath $archivePath -Force }
New-Item -ItemType Directory -Force -Path $stageRoot | Out-Null

foreach ($directory in @(".codex-plugin", "skills")) {
    Copy-Item -LiteralPath (Join-Path $pluginRoot $directory) -Destination $stageRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path (Join-Path $stageRoot "scripts") | Out-Null
foreach ($script in @("start-plugin.ps1", "stop-plugin.ps1", "connect-harness.ps1")) {
    Copy-Item -LiteralPath (Join-Path $pluginRoot "scripts\$script") -Destination (Join-Path $stageRoot "scripts") -Force
}
New-Item -ItemType Directory -Force -Path (Join-Path $stageRoot "backend") | Out-Null
Copy-Item -LiteralPath (Join-Path $pluginRoot "backend\apiswitch") -Destination (Join-Path $stageRoot "backend") -Recurse -Force
$omittedBackendPaths = @(
    "backend\apiswitch\agents",
    "backend\apiswitch\services\agent_configs.py",
    "backend\apiswitch\schemas\agents.py",
    "backend\apiswitch\desktop.py",
    "backend\apiswitch\desktop_entry.py",
    "backend\apiswitch\desktop_diagnostics.py"
)
foreach ($relativePath in $omittedBackendPaths) {
    $target = Join-Path $stageRoot $relativePath
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
}
New-Item -ItemType Directory -Force -Path (Join-Path $stageRoot "frontend") | Out-Null
Copy-Item -LiteralPath (Join-Path $pluginRoot "frontend\dist") -Destination (Join-Path $stageRoot "frontend") -Recurse -Force
foreach ($file in @("README.md", "requirements-plugin.txt")) {
    Copy-Item -LiteralPath (Join-Path $pluginRoot $file) -Destination $stageRoot -Force
}

Get-ChildItem -LiteralPath $stageRoot -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $stageRoot -Recurse -File -Include "*.pyc", "*.pyo" | Remove-Item -Force
Compress-Archive -LiteralPath $stageRoot -DestinationPath $archivePath -CompressionLevel Optimal
[pscustomobject]@{
    archive = $archivePath
    version = $manifest.version
    size = (Get-Item -LiteralPath $archivePath).Length
} | ConvertTo-Json
