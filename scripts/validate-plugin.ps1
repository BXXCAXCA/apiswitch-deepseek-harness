[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$pluginRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$manifestPath = Join-Path $pluginRoot ".codex-plugin\plugin.json"
$skillPath = Join-Path $pluginRoot "skills\apiswitch-deepseek-harness\SKILL.md"

if (-not (Test-Path -LiteralPath $manifestPath)) { throw "Missing .codex-plugin/plugin.json" }
if (-not (Test-Path -LiteralPath $skillPath)) { throw "Missing plugin skill" }
$manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
if ($manifest.name -ne "apiswitch-deepseek-harness") { throw "Plugin name does not match its folder." }
if ($manifest.version -notmatch '^\d+\.\d+\.\d+([+-][0-9A-Za-z.-]+)?$') { throw "Plugin version is not strict semver." }
foreach ($field in @("description", "author", "interface", "skills")) {
    if (-not $manifest.$field) { throw "Plugin manifest is missing $field." }
}
$skill = Get-Content -Raw -LiteralPath $skillPath
if ($skill -notmatch '(?s)^---\s+name:\s*apiswitch-deepseek-harness\s+description:.+?---') {
    throw "Plugin skill frontmatter is invalid."
}
if ($skill -match '\[TODO:') { throw "Plugin contains a TODO placeholder." }
[pscustomobject]@{ valid = $true; name = $manifest.name; version = $manifest.version } | ConvertTo-Json
