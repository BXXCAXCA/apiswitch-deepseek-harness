---
name: apiswitch-deepseek-harness
description: Start and manage the local APISwitch gateway used by DeepSeek Harness, connect a workspace through its .env file, and open the provider/model routing UI. Use when the user asks to run DeepSeek Harness through APISwitch, switch or combine Harness model providers, inspect routing failures, or stop the local Harness gateway.
---

# APISwitch for DeepSeek Harness

Use the scripts in this plugin to run APISwitch as a loopback-only model gateway for DeepSeek
Harness. The plugin owns one local gateway credential, so there is no client-management or generic
Agent-configuration workflow.

## Resolve the plugin root

This skill lives at `skills/apiswitch-deepseek-harness/SKILL.md`. Resolve the plugin root as the
directory two levels above this skill directory. Run PowerShell scripts from `<plugin-root>/scripts`.

## Start and inspect

1. Run `scripts/start-plugin.ps1`. It is idempotent and returns JSON containing `web_url`,
   `openai_base_url`, `api_key_file`, `process_id`, and `port`.
2. Show `web_url` as a clickable Markdown link. Do not open a browser unless the user explicitly
   asks.
3. Never print or quote the contents of `api_key_file`. It is a local secret consumed by the
   connection script.
4. In the Web UI, configure providers, upstream models, unified models, auxiliary models, routing,
   budgets, logs, accounting, and settings. Client management and generic Agent configuration are
   intentionally absent.

The first source checkout start may create a private virtual environment and build the frontend.
Report missing Python 3.11+ or Node.js/npm prerequisites clearly.

## Connect a workspace

Only connect a workspace when the user asks to use APISwitch with DeepSeek Harness or authorizes
configuration of that workspace.

1. Run `scripts/connect-harness.ps1 -Workspace <absolute-workspace-path>`.
2. State that the script updates only `DEEPSEEK_BASE_URL` and `DEEPSEEK_API_KEY` in the workspace's
   untracked `.env` file, preserving other lines.
3. If the separate `deepseek-harness` plugin is available, follow its skill for `doctor`, starting a
   visible session, waiting, and independently reviewing changes. APISwitch supplies the model
   endpoint; it does not replace Harness session control.

## Stop

Run `scripts/stop-plugin.ps1` when the user asks to stop the gateway. The script only terminates the
recorded `python -m apiswitch` process and removes its runtime record; provider/model data and credentials
remain in the plugin data directory.

## Safety

- Keep the service bound to `127.0.0.1`.
- Do not reveal the Harness gateway key in chat or logs.
- Do not modify provider credentials unless the user asks.
- Do not delete the plugin data directory as part of a normal stop.
