[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8080,
    [switch]$RebuildFrontend,
    [switch]$UseCurrentPython
)

$ErrorActionPreference = "Stop"
$pluginRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$backendDir = Join-Path $pluginRoot "backend"
$frontendDir = Join-Path $pluginRoot "frontend"
$dataRoot = if ($env:APISWITCH_HARNESS_DATA_DIR) {
    [IO.Path]::GetFullPath($env:APISWITCH_HARNESS_DATA_DIR)
} else {
    Join-Path $env:LOCALAPPDATA "APISwitchDeepSeekHarness"
}
$runtimePath = Join-Path $dataRoot "runtime.json"
$tokenPath = Join-Path $dataRoot "harness.token"
$masterKeyPath = Join-Path $dataRoot "master.key"
$stdoutPath = Join-Path $dataRoot "gateway.stdout.log"
$stderrPath = Join-Path $dataRoot "gateway.stderr.log"
$databasePath = Join-Path $dataRoot "apiswitch.db"
$filesPath = Join-Path $dataRoot "files"
$venvPath = Join-Path $dataRoot "venv"
$venvPython = Join-Path $venvPath "Scripts\python.exe"
$dependencyMarker = Join-Path $venvPath "requirements.sha256"
$frontendIndex = Join-Path $frontendDir "dist\index.html"

New-Item -ItemType Directory -Force -Path $dataRoot, $filesPath | Out-Null

function Test-Health([int]$CandidatePort) {
    try {
        $result = Invoke-RestMethod -Uri "http://127.0.0.1:$CandidatePort/health" -TimeoutSec 2
        return $result.status -eq "ok" -and $result.service -eq "apiswitch"
    } catch {
        return $false
    }
}

function Test-PortAvailable([int]$CandidatePort) {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $CandidatePort)
    try {
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        $listener.Stop()
    }
}

function Find-Python {
    $candidates = @()
    if ($env:APISWITCH_PLUGIN_PYTHON) { $candidates += $env:APISWITCH_PLUGIN_PYTHON }
    $localPython = Join-Path $backendDir ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $localPython) { $candidates += $localPython }
    foreach ($name in @("python", "py")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) { $candidates += $command.Source }
    }
    foreach ($candidate in $candidates | Select-Object -Unique) {
        & $candidate --version *> $null
        if ($LASTEXITCODE -eq 0) { return $candidate }
    }
    throw "Python 3.11+ is required. Set APISWITCH_PLUGIN_PYTHON to a working python.exe."
}

if (Test-Path -LiteralPath $runtimePath) {
    $existing = Get-Content -Raw -LiteralPath $runtimePath | ConvertFrom-Json
    if ($existing.port -and (Test-Health ([int]$existing.port))) {
        [pscustomobject]@{
            status = "running"
            web_url = "http://127.0.0.1:$($existing.port)/ui/"
            openai_base_url = "http://127.0.0.1:$($existing.port)/v1"
            api_key_file = $tokenPath
            process_id = $existing.process_id
            port = [int]$existing.port
        } | ConvertTo-Json
        exit 0
    }
}

$sourcePython = Find-Python
$requirementsPath = Join-Path $pluginRoot "requirements-plugin.txt"
if ($UseCurrentPython) {
    $venvPython = $sourcePython
} else {
    if (-not (Test-Path -LiteralPath $venvPython)) {
        & $sourcePython -m venv $venvPath
        if ($LASTEXITCODE -ne 0) { throw "Failed to create the plugin Python environment." }
    }
    $requirementsHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $requirementsPath).Hash
    $installedHash = if (Test-Path -LiteralPath $dependencyMarker) {
        (Get-Content -Raw -LiteralPath $dependencyMarker).Trim()
    } else { "" }
    if ($requirementsHash -ne $installedHash) {
        & $venvPython -m pip install --disable-pip-version-check -r $requirementsPath
        if ($LASTEXITCODE -ne 0) { throw "Failed to install APISwitch plugin dependencies." }
        Set-Content -LiteralPath $dependencyMarker -Value $requirementsHash -Encoding ascii
    }
}

if ($RebuildFrontend -or -not (Test-Path -LiteralPath $frontendIndex)) {
    $npm = (Get-Command npm -ErrorAction Stop).Source
    Push-Location $frontendDir
    try {
        if (-not (Test-Path -LiteralPath (Join-Path $frontendDir "node_modules"))) {
            & $npm ci
            if ($LASTEXITCODE -ne 0) { throw "Frontend dependency installation failed." }
        }
        & $npm run build
        if ($LASTEXITCODE -ne 0) { throw "Frontend build failed." }
    } finally {
        Pop-Location
    }
}

if (-not (Test-Path -LiteralPath $masterKeyPath)) {
    $masterKey = (& $venvPython -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())").Trim()
    Set-Content -LiteralPath $masterKeyPath -Value $masterKey -Encoding ascii
}
$masterKey = (Get-Content -Raw -LiteralPath $masterKeyPath).Trim()

$selectedPort = $Port
while (-not (Test-PortAvailable $selectedPort)) {
    $selectedPort++
    if ($selectedPort -gt 65535) { throw "No loopback port is available." }
}

$previous = @{
    APISWITCH_PLUGIN_MODE = $env:APISWITCH_PLUGIN_MODE
    APISWITCH_LISTEN_HOST = $env:APISWITCH_LISTEN_HOST
    APISWITCH_PORT = $env:APISWITCH_PORT
    APISWITCH_DATABASE_URL = $env:APISWITCH_DATABASE_URL
    APISWITCH_FILE_STORAGE_DIR = $env:APISWITCH_FILE_STORAGE_DIR
    APISWITCH_FRONTEND_DIST_DIR = $env:APISWITCH_FRONTEND_DIST_DIR
    APISWITCH_HARNESS_TOKEN_FILE = $env:APISWITCH_HARNESS_TOKEN_FILE
    APISWITCH_MASTER_KEY = $env:APISWITCH_MASTER_KEY
}
try {
    $env:APISWITCH_PLUGIN_MODE = "true"
    $env:APISWITCH_LISTEN_HOST = "127.0.0.1"
    $env:APISWITCH_PORT = "$selectedPort"
    $env:APISWITCH_DATABASE_URL = "sqlite:///$($databasePath.Replace('\', '/'))"
    $env:APISWITCH_FILE_STORAGE_DIR = $filesPath
    $env:APISWITCH_FRONTEND_DIST_DIR = Join-Path $frontendDir "dist"
    $env:APISWITCH_HARNESS_TOKEN_FILE = $tokenPath
    $env:APISWITCH_MASTER_KEY = $masterKey
    Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    $process = Start-Process -FilePath $venvPython -ArgumentList "-m", "apiswitch" -WorkingDirectory $backendDir -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
} finally {
    foreach ($item in $previous.GetEnumerator()) {
        if ($null -eq $item.Value) {
            Remove-Item -Path "Env:$($item.Key)" -ErrorAction SilentlyContinue
        } else {
            Set-Item -Path "Env:$($item.Key)" -Value $item.Value
        }
    }
}

$ready = $false
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    Start-Sleep -Milliseconds 500
    if ($process.HasExited) { break }
    if (Test-Health $selectedPort) { $ready = $true; break }
}
if (-not $ready) {
    if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force }
    $details = if (Test-Path -LiteralPath $stderrPath) {
        (Get-Content -LiteralPath $stderrPath -Tail 30) -join [Environment]::NewLine
    } else { "No stderr log was created." }
    throw "APISwitch Harness gateway did not become healthy on port $selectedPort.`n$details"
}

[pscustomobject]@{
    process_id = $process.Id
    port = $selectedPort
    started_at = [DateTimeOffset]::Now.ToString("o")
    plugin_root = $pluginRoot
} | ConvertTo-Json | Set-Content -LiteralPath $runtimePath -Encoding utf8

[pscustomobject]@{
    status = "started"
    web_url = "http://127.0.0.1:$selectedPort/ui/"
    openai_base_url = "http://127.0.0.1:$selectedPort/v1"
    api_key_file = $tokenPath
    process_id = $process.Id
    port = $selectedPort
} | ConvertTo-Json
