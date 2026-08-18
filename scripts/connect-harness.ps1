[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Workspace
)

$ErrorActionPreference = "Stop"
$workspacePath = (Resolve-Path -LiteralPath $Workspace).Path
$dataRoot = if ($env:APISWITCH_HARNESS_DATA_DIR) {
    [IO.Path]::GetFullPath($env:APISWITCH_HARNESS_DATA_DIR)
} else {
    Join-Path $env:LOCALAPPDATA "APISwitchDeepSeekHarness"
}
$runtimePath = Join-Path $dataRoot "runtime.json"
$tokenPath = Join-Path $dataRoot "harness.token"
if (-not (Test-Path -LiteralPath $runtimePath) -or -not (Test-Path -LiteralPath $tokenPath)) {
    throw "Start the APISwitch Harness plugin before connecting a workspace."
}
$runtime = Get-Content -Raw -LiteralPath $runtimePath | ConvertFrom-Json
$apiKey = (Get-Content -Raw -LiteralPath $tokenPath).Trim()
$envPath = Join-Path $workspacePath ".env"
$lines = if (Test-Path -LiteralPath $envPath) { @(Get-Content -LiteralPath $envPath) } else { @() }
$lines = @($lines | Where-Object { $_ -notmatch '^\s*DEEPSEEK_(BASE_URL|API_KEY)\s*=' })
$lines += "DEEPSEEK_BASE_URL=http://127.0.0.1:$($runtime.port)/v1"
$lines += "DEEPSEEK_API_KEY=$apiKey"
Set-Content -LiteralPath $envPath -Value $lines -Encoding utf8
[pscustomobject]@{
    status = "connected"
    workspace = $workspacePath
    env_file = $envPath
    base_url = "http://127.0.0.1:$($runtime.port)/v1"
} | ConvertTo-Json
