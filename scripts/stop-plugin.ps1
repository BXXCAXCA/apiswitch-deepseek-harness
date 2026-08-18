[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$dataRoot = if ($env:APISWITCH_HARNESS_DATA_DIR) {
    [IO.Path]::GetFullPath($env:APISWITCH_HARNESS_DATA_DIR)
} else {
    Join-Path $env:LOCALAPPDATA "APISwitchDeepSeekHarness"
}
$runtimePath = Join-Path $dataRoot "runtime.json"
if (-not (Test-Path -LiteralPath $runtimePath)) {
    [pscustomobject]@{ status = "stopped"; message = "No plugin runtime is recorded." } | ConvertTo-Json
    exit 0
}

$runtime = Get-Content -Raw -LiteralPath $runtimePath | ConvertFrom-Json
$process = Get-CimInstance Win32_Process -Filter "ProcessId = $($runtime.process_id)" -ErrorAction SilentlyContinue
if ($process -and $process.CommandLine -match "-m\s+apiswitch(?:\s|$)") {
    Stop-Process -Id ([int]$runtime.process_id) -Force
}
Remove-Item -LiteralPath $runtimePath -Force
[pscustomobject]@{ status = "stopped"; process_id = $runtime.process_id } | ConvertTo-Json
