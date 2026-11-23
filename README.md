# hermes-warm-acp

Keep-warm ACP client for [Hermes Agent](https://hermes-agent.nousresearch.com) model-provider plugins.

## The problem

Hermes' generic `ACPClient` spawns a **fresh agent-harness subprocess per chat turn**: spawn → `initialize` → `session/new` → prompt → kill. Measured on Mistral's `vibe-acp` (mistral-vibe 3.1.4), that handshake chain costs **~4.8–5.0s of pure overhead on every message**:

| Step (cold turn) | ms |
|---|---:|
| OS fork/exec | ~2 |
| `initialize` RPC | ~2,400 |
| `session/new` | ~1,800 (≈1,300 warm) |
| model select | ~600 |
+ model inference (varies, 1.2–20s under Mistral load)

The ACP protocol was designed for the opposite: **the editor boots the agent once and runs many sessions on that one process** (Zed, JetBrains). The harnesses want to be reused — the per-turn teardown is a client-side habit, not a protocol limit.

## The fix

`WarmACPClient` (see `mistral_vibe_acp_provider.py`) keeps **one long-lived harness process per slot**:

- `initialize` **once** per process
- a **fresh ACP session per turn** (`session/new` → model select → `session/prompt` → `session/close`) — context flow is identical to the cold path, so the host's checkpoint-pruned active window is still what crosses the wire
- any request error (billing 402/2304, capacity 429/3505, auth 401) **poisons the slot** → killed → next checkout respawns through the key rotator (next live pool key)
- `is_closed` stays `False` while a warm worker is idle, so host lifecycle code doesn't `close()` the pool after turn 1
- `close()` tears the pool down

Warm turns measure **~2.6–4.8s total** (session/new ~1.3s + model reply) vs ~5s of setup overhead + reply on the cold path. `list_models()` over the warm process: **~0.9s** (was a full throwaway spawn).

## Crash recovery

SIGKILLed the warm process mid-life → next turn auto-respawned and answered in 10.5s (one-time cold cost). Idle workers found dead at checkout are dropped and respawned.

## Design constraints honored (from the live repo)

- **Zero changes to the generic `ACPClient`** — this is a plugin-side facade that reuses the base class's tested request plumbing, message bridge (`_handle_server_message`), and formatter. The base class's invariant tests (31/31) stay green; every other ACP provider keeps today's behavior.
- **Don't reuse one ACP session across turns** unless you also change prompt formatting — resending the full transcript into a session that already holds it doubles the history.
- Key rotation cannot swap a key inside a live harness process (auth is baked into env at spawn) — hence poison-and-respawn, not in-place rotation.

## Files

- `mistral_vibe_acp_provider.py` — drop-in Hermes provider plugin (`model-providers/mistral-vibe-acp/__init__.py`), including the key-rotator wiring (`vibe_acp_rotator.py`, not included: selects the next live key from the host's credential pool, spawns the harness with it, benches+rotates on 401/402/429)

## Porting to another ACP harness

1. Point `command`/`args` at your harness (directly, or through a wrapper if it reads a key from env).
2. Confirm the harness supports `session/close` (ACP v1: idle-only, idempotent). If not, drop the close call and accept session accumulation, or reap via the harness's delete extension.
3. Keep per-turn session freshness; only the PROCESS is warm.

## Measured numbers

Live battery (2026-11-23, real pool keys, real prompts):

- cold turn (fresh worker, initialize included): 8.4–13.2s
- warm turns: 2.6 / 4.2 / 4.8s
- after SIGKILL of the warm proc (respawn + cold): 10.5s
- warm `list_models`: 0.9–1.3s
- model-time variance (1.2–20s on identical prompts) is backend capacity, not ACP overhead
