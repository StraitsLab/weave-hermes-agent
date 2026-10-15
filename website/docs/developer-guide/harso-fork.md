---
title: Harso fork settings
description: Configuration keys that exist only in the StraitsLab Harso fork of Hermes.
---

# Harso fork settings

This page lists configuration keys that the StraitsLab Harso fork adds to Hermes.
Upstream Hermes does not read them. Each Harso feature adds its keys here, one line per dotted path.

| Key | What it does |
|---|---|
| `agent.engine_identity` | Stamp the engine identity block into the system prompt. Default `true`. |
| `agent.identity_epoch_rebuild` | Rebuild the cached system prompt when the identity epoch changes. Default `false`. |
| `browser.web_bot_auth` | Sign browser requests with Web Bot Auth through the signer hook. Default `false`. |
| `cron.allow_scripts` | Allow cron jobs that run a script. Default `true`. |
| `cron.platform_script_root` | Root-owned folder that holds platform routine scripts. Empty = none. |
| `cron.platform_script_digests` | Allowed SHA-256 digests of platform routine scripts, by file name. |
| `tools.tool_search.always_eager` | Tool names that stay in the model's tool list when tool search defers the rest. |
| `memory.external_prefetch_timeout` | Seconds an external memory provider may spend on per-turn prefetch. Default `1.0`. |
