# APISwitch for DeepSeek Harness

APISwitch for DeepSeek Harness is a plugin-focused fork of
[APISwitch](https://github.com/BXXCAXCA/apiswitch). It runs a loopback-only, multi-provider AI
gateway for DeepSeek Harness and keeps the provider/model routing controls that matter to Harness.

Version: `0.1.0`

## What changed from APISwitch

- Added a Codex plugin manifest and a dedicated `apiswitch-deepseek-harness` skill.
- Added idempotent plugin start, workspace connection, and stop scripts.
- Added a plugin-managed local Harness API key. It automatically has access to every enabled
  unified model, including models created after first startup.
- Removed the Client Management and generic Agent Configuration pages from the plugin UI.
- Kept provider instances, upstream models, unified models, auxiliary workflows, routing, budgets,
  logs, accounting, protocol conversion, and privacy settings.
- Uses `%LOCALAPPDATA%\APISwitchDeepSeekHarness` by default, separate from desktop APISwitch data.

## Architecture

```text
DeepSeek Harness
       |
       | OpenAI-compatible request + plugin-managed local key
       v
APISwitch Harness gateway (127.0.0.1 only)
       |
       +-- unified model routing and failover
       +-- auxiliary vision/file/audio workflows
       +-- budgets, usage, logs, and structured errors
       |
       v
DeepSeek / GLM / Qwen / compatible providers
```

## Run from source

Requirements: Windows PowerShell and Python 3.11+. Node.js/npm is needed only when starting from a
source checkout without a prebuilt `frontend/dist` directory.

```powershell
.\scripts\start-plugin.ps1
```

The command returns JSON with the local management UI and gateway addresses. The plaintext Harness
key is not printed.

Connect a workspace:

```powershell
.\scripts\connect-harness.ps1 -Workspace C:\absolute\path\to\workspace
```

This updates only these entries in the workspace's `.env` file:

```dotenv
DEEPSEEK_BASE_URL=http://127.0.0.1:<port>/v1
DEEPSEEK_API_KEY=<plugin-managed-local-key>
```

Stop the gateway without deleting configuration:

```powershell
.\scripts\stop-plugin.ps1
```

## Configure models

1. Add one or more provider instances.
2. Pull or add their upstream models.
3. Create Harness-facing unified model names, such as `deepseek-v4-pro` or
   `deepseek-v4-flash`.
4. Bind candidates from any provider and drag them into failover priority order.
5. Optionally add vision, file, audio, or context auxiliary workflows.
6. Connect the target workspace and start DeepSeek Harness normally.

The dedicated Harness key sees all enabled unified models automatically. No client token or generic
Agent configuration step is required.

## Development checks

```powershell
python -m pytest backend/tests -q
python -m ruff check backend --select F,E9
Set-Location frontend
npm ci
npm run test
npm run build
```

The GitHub Actions workflow validates the backend, frontend, sensitive-data scan, plugin manifest,
and distributable plugin archive.
