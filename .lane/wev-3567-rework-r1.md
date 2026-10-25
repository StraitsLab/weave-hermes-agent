# WEV-3567 review rework, round 1

Fix base: `0e1253d1abdaa764e0d1ddd0c21c7ecd540c704e`.

## Requirements and classes

- F1: per-chat admission must never change profile defaults. Bare model input delegated persistence to the user's default. Force stock `--session` on both selection and rollback. Variants considered: persist default false (existing), true (new regression), another live session on the same profile (new), rollback under persist true (new).
- F2: a model ID is not a complete route identity. Stock aliases and configured catalogs can change provider or URL. Capture the live route, compare model/provider/URL after switching, restore the previous model in session scope and refuse dispatch on mismatch. Variants: competing configured provider, direct alias reverse match with identical model/provider and changed URL, successful unchanged route, confirmation/deferred/no-op/RPC failure (existing). Rollback supplies the old provider when its label changed; otherwise normal resolution restores the old model endpoint.
- F3: admission reads must not flush usage or load prompts/full session rows. Select only model_config by exact session ID. Variants: known locked row, missing row, forbidden full-row reader, forbidden usage flush.
- F4: GET's shared client-safe serializer must project only confirmed lock model/effort/fast. Variants: absent lock, unconfirmed lock, confirmed default options, confirmed explicit options, private model_config siblings excluded.

## Red/green

Six new regression cases ran with all three production files restored to the reviewed head: **6 failed, 31 deselected**. Failures were full-row read, missing narrow reader, global persist called, two reroutes silently accepted, missing `model_lock` key. Fixed bytes restored exactly, same selection: **6 passed, 31 deselected**.

Raw logs and the bounded restore script are scratch-only under `cache/scratch/mp/wev-3567-review-fix/` and `cache/scratch/mp/wev-3567-red-green.py`.

## Pre-review hunt

- Siblings: shared serializer serves GET/create/list/patch/fork. Native no-lock/busy paths unchanged; legacy MessageEvent remains out of this native-only patch. Reasoning and fast remain session scoped through existing handlers.
- Lifetime: same `_before_dispatch` settlement and pre-ownership admission boundary; no running session is reconfigured. A mismatch never calls prompt.submit.
- Regression found during hunt: stock startup canonicalizes a named custom route to `custom`, then switching can relabel it `custom:fixture`. Strict route preservation correctly refuses that. Smoke now declares the same fixture endpoint under the actual live provider and publishes all three model IDs; first and later swaps retain the live label.
- No new deadlines, production knobs, network clients, or secret logging. Temporary diagnostics removed. Deslop inspected all changed hunks; no behavior-neutral cleanup required.
- Test audit: stock parser/handler/switch are exercised, only external provider I/O and agent execution stubbed; API projection exercised via HTTP. Narrow-reader test separately owns the no-flush/narrow-result contract.

## Validation

- Canonical runner: native model lock **16 passed** (includes HTTP -> actual inference), safe fork **6 passed**, fast scope **6 passed**, reasoning scope **8 passed**; **36 total**.
- Clean-env pytest with existing scratch-only pytest-asyncio overlay: awake admission + submit API + session API **110 passed** (session API **21**).
- Standalone model-lock smoke: **passed**, two picks reached native inference.
- Existing full bot-admission suite: **3 passed, 1 failed**. Full smoke likewise fails because the delivery child cannot import `hermes_yaml`, the documented pre-existing baseline. No claim that full bot messaging is green.
- `git diff --check`: passed before commit.

No merge or deployment. Pre-existing contributor-email file change left unstaged.
