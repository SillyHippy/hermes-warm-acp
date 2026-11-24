# Hermes Mistral Vibe ACP

Complete Hermes provider for Mistral Vibe ACP, including the warm ACP session client and Vibe-key rotator.

This is one Mistral-specific package. It is not a generic ACP framework.

## What it does

Hermes talks to Mistral's `vibe-acp` harness over ACP. The provider keeps one warm harness process, opens a fresh ACP session for each turn, and rotates across the `mistral-vibe` credential pool when a key hits auth, billing, or capacity failure.

Measured cold handshake overhead is about 4.8–5.0 seconds:

- process spawn: about 2 ms
- `initialize`: about 2,400 ms
- `session/new`: about 1,800 ms cold, about 1,300 ms warm
- model select: about 600 ms

The warm client pays `initialize` once per process. Later turns pay `session/new`, model select, and model time.

## Install

Install the harness:

```bash
uv tool install --python 3.14 mistral-vibe
```

Copy these paths onto the Hermes box:

- `model-providers/mistral-vibe-acp/` to `~/.hermes/plugins/model-providers/mistral-vibe-acp/`
- `scripts/vibe_acp_rotator.py` to `~/.hermes/scripts/vibe_acp_rotator.py`

The plugin command is pinned to the Hermes venv interpreter because the rotator imports Hermes' credential pool and `ruamel.yaml`. If that interpreter path differs on another box, change `_COMMAND` in `model-providers/mistral-vibe-acp/__init__.py` to the interpreter that can import `ruamel`.

Add the Vibe pool keys with `hermes auth add`, under provider `mistral-vibe`.

Add this provider block:

```yaml
providers:
  mistral-vibe-acp:
    name: Mistral Vibe ACP
    base_url: acp://mistral-vibe-acp
    discover_models: false
    enabled: true
    request_timeout_seconds: 300
    stale_timeout_seconds: 300
    models:
      - mistral-vibe-acp/mistral-large-4-0
      - mistral-vibe-acp/zai-glm-5-3
      - mistral-vibe-acp/zai-glm-5-3-flash
      - mistral-vibe-acp/mistral-medium-latest
      - mistral-vibe-acp/mistral-medium-3.5
      - mistral-vibe-acp/deepseek-v4-1-flash-1017
```

## Verify

```bash
hermes chat --provider mistral-vibe-acp -m mistral-vibe-acp/mistral-large-4-0 -q 'Reply with exactly: ACP_FIX_OK' -Q
```

Expected: `ACP_FIX_OK`.

## Repository layout

- `model-providers/mistral-vibe-acp/__init__.py` — Hermes provider, warm client, image-path bridge, and stderr capture
- `model-providers/mistral-vibe-acp/plugin.yaml` — plugin manifest
- `scripts/vibe_acp_rotator.py` — pool-key selector and `vibe-acp` launcher
- `mistral_vibe_acp_provider.py` and `vibe_acp_rotator.py` — compatibility copies of the same two Python files

## Warm-session rules

- Keep the harness process warm.
- Start a fresh ACP session for every turn.
- Do not reuse one ACP session across turns unless prompt formatting also changes. Resending a full transcript into a session that already holds it duplicates history.
- A bad key cannot be swapped inside a live process. Kill that slot and respawn with the next pool key.
- Keep child stderr. The warm slot used to discard it, which hid the real exit reason.

## Interpreter failure

If the gateway runs Python 3.14 but the Hermes venv is Python 3.11, spawning the rotator with plain `python3` can fail before `initialize`:

```text
ModuleNotFoundError: No module named 'ruamel'
```

The shipped plugin avoids that by using:

```text
/usr/local/lib/hermes-agent/venv/bin/python3
```

## Verified

On 2026-11-24, a live Discord turn on `mistral-vibe-acp/mistral-large-4-0` completed on the ACP provider itself: one API call, `finish_reason=stop`, no fallback.

Earlier measurements from 2026-11-23:

- cold turn: 8.4–13.2 seconds
- warm turns: 2.6, 4.2, and 4.8 seconds
- SIGKILL recovery: 10.5 seconds
- warm `list_models`: 0.9–1.3 seconds
