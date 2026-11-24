"""Mistral Vibe ACP provider — Hermes drives the real ``vibe-acp`` harness over ACP stdio.

Why ACP instead of the API key: as of the December 2026 billing cycle, Mistral
rebuckets calls from non-Mistral clients (raw API-key usage) onto the monthly
API allowance, but explicitly keeps "Mistral harnesses via ACP with third-party
tools" on the Vibe allowance (the Pro/Education pool). ``vibe-acp`` *is* the
Mistral harness, so this path stays on the Vibe allowance.

Key rotation: the harness reads one MISTRAL_API_KEY, so Hermes spawns it through
``scripts/vibe_acp_rotator.py``, which selects from the mistral-vibe credential
pool (the six paid Vibe keys, fill_first) and benches+rotates on 401/402/429.

KEEP-WARM (2026-11-23): fleet research (Joseph bot design + live timing child)
proved ~4.8-5.0s of every turn is handshake overhead (initialize ~2.4s,
session/new ~1.8s cold). This profile returns ``WarmACPClient``, which keeps ONE
long-lived rotator+vibe-acp process across turns: spawn+initialize once, then a
fresh ACP session per turn (session/new -> model select -> prompt -> close).
Context flow is identical to the cold path (Hermes sends the pruned active
window every turn) — only the spawn/handshake cost is gone.

Failure handling: any request error poisons the worker — slot killed, next
checkout respawns through the rotator, which picks the next live pool key.
``is_closed`` stays False while a warm worker exists (lifecycle code would
otherwise close() the pool right after the first turn). ``close()`` tears down.
Opt-in to THIS provider only — the generic ACPClient and its invariant tests
are untouched, so every other ACP seat keeps today's behavior.
"""

from __future__ import annotations

import contextlib
import json
import queue
import threading
import time
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

_COMMAND = "python3"
_ARGS = ("/root/.hermes/scripts/vibe_acp_rotator.py",)

# Errors that mean the WORKER's key is bad (rotate) rather than the prompt bad.
_POISON_MARKERS = ("billing_vibe_budget_exhausted", "backend_out_of_capacity", "2304", "3505", "HTTP 401", "HTTP 402", "HTTP 429")


def _prefixed(model: str) -> str:
    return f"mistral-vibe-acp/{model}"


# Best-guess catalog for the picker before a live session advertises models.
_FALLBACK_MODELS = (
    _prefixed("zai-glm-5-3"),
    _prefixed("zai-glm-5-3-flash"),
    _prefixed("mistral-large-4-0"),
    _prefixed("mistral-medium-latest"),
    _prefixed("mistral-medium-3.5"),
    _prefixed("deepseek-v4-1-flash-1017"),
)


def _is_poisonous(error_text: str) -> bool:
    return any(marker in error_text for marker in _POISON_MARKERS)


class _WarmSlot:
    """One live rotator(+vibe-acp) process with process-lifetime IO plumbing."""

    def __init__(self, proc) -> None:
        self.proc = proc
        self.inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        self.request_ids = iter(range(1, 1 << 62))
        self.initialized = False
        self.busy = False
        self.last_used = time.monotonic()

        def _decode(line: str) -> dict[str, Any]:
            try:
                return json.loads(line)
            except Exception:
                return {"raw": line.rstrip("\n")}

        def _pump(stream, sink) -> None:
            try:
                for line in stream or ():
                    sink(line)
            except Exception:
                return

        threading.Thread(target=_pump, args=(proc.stdout, lambda line: self.inbox.put(_decode(line))), daemon=True).start()
        threading.Thread(target=_pump, args=(proc.stderr, lambda line: None), daemon=True).start()


class WarmACPClient:
    """OpenAI-shaped facade over a keep-warm ACP process for one provider.

    Drives the tested base-class session plumbing (monkey-patched onto the warm
    proc) so JSON-RPC request semantics, fs/permission bridges, error strings
    and retry classification stay identical to the cold path.
    """

    def __init__(self, *, command: str, args: list[str], pool_size: int = 1, **client_kwargs: Any):
        self._template_kwargs: dict[str, Any] = dict(client_kwargs)
        self._template_kwargs["command"] = command
        self._template_kwargs["args"] = list(args)
        self._pool_size = max(1, int(pool_size))
        self._lock = threading.RLock()
        self._slots: list[_WarmSlot] = []
        self.is_closed = False
        self._active_processes: set = set()
        self._make_chat_facade()
        self._spawn_count = 0

    # -- template ----------------------------------------------------------
    def _template(self) -> Any:
        """Bare base client that owns label/hints/bridge mechanics; never spawns."""
        from agent.acp_client import ACPClient, _INITIALIZE_PARAMS

        tpl = ACPClient(**self._template_kwargs)
        tpl.requires_initializer = lambda: False
        tpl.early_exit_error = None
        # ensure it can never spawn: freeze _spawn
        tpl._spawn = self._frozen_spawn
        return tpl

    def _frozen_spawn(self, *a, **k):
        raise RuntimeError("template client must never spawn")

    def _make_chat_facade(self) -> None:
        from types import SimpleNamespace

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create_chat_completion))

    # -- slot pool ---------------------------------------------------------
    def _acquire_slot(self) -> _WarmSlot:
        with self._lock:
            for slot in self._slots:
                if not slot.busy:
                    if slot.proc.poll() is None:
                        slot.busy = True
                        slot.last_used = time.monotonic()
                        return slot
                    self._drop_slot_locked(slot)  # dead idle worker
            if len(self._slots) < self._pool_size:
                slot = self._spawn_slot()
                self._slots.append(slot)
                slot.busy = True
                return slot
            raise TimeoutError("Warm ACP pool exhausted")
        raise TimeoutError("unreachable")

    def _drop_slot_locked(self, slot: _WarmSlot) -> None:
        with contextlib.suppress(ValueError):
            self._slots.remove(slot)
        self._terminate_proc(slot.proc)

    def _spawn_slot(self) -> _WarmSlot:
        """Spawn a fresh warm process using the base client's real _spawn."""
        from agent.acp_client import ACPClient

        tmp = ACPClient(**self._template_kwargs)
        if not hasattr(tmp, "_spawn"):
            raise RuntimeError("ACPClient spawn unavailable")
        proc = tmp._spawn()
        self._active_processes.add(proc)
        self._spawn_count += 1
        return _WarmSlot(proc)

    def _terminate_proc(self, proc) -> None:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            with contextlib.suppress(Exception):
                proc.kill()
        with contextlib.suppress(KeyError):
            self._active_processes.discard(proc)

    def _release_slot(self, slot: _WarmSlot, *, healthy: bool = True) -> None:
        with self._lock:
            slot.busy = False
            if not healthy or slot.proc.poll() is not None:
                self._drop_slot_locked(slot)

    def _all_procs(self):
        return [s.proc for s in self._slots]

    # -- warm request driver -----------------------------------------------
    def _drive_session(
        self, *, timeout_seconds: float, model: str | None = None, prompt_text: str | None = None,
        allow_file_requests: bool = True, discovery: bool = False, on_chunk=None,
    ) -> tuple[Any, ...]:
        """Run one ACP session on a warm proc: new session → model → prompt → close.

        ``on_chunk``: optional callback invoked with each live agent text chunk
        (str) as it arrives over session/update — implements real streaming.
        """
        from agent.acp_client import (
            _INITIALIZE_PARAMS, _model_selection_request, _session_model_ids, logger,
        )

        tpl = self._template()
        slot = self._acquire_slot()
        proc = slot.proc
        deadline = time.monotonic() + timeout_seconds
        healthy = True
        try:
            request_ids = slot.request_ids
            inbox = slot.inbox
            template_wait_ctx = None

            def _request(method: str, params: dict[str, Any], *, text_parts=None, reasoning_parts=None,
                         deadline_override: float | None = None) -> Any:
                request_id = next(request_ids)
                try:
                    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
                    proc.stdin.flush()
                except OSError:
                    if proc.poll() is None:
                        raise
                wait_deadline = deadline if deadline_override is None else deadline_override
                while time.monotonic() < wait_deadline and proc.poll() is None:
                    try:
                        msg = inbox.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if self._handle(msg, process=proc, cwd=tpl._acp_cwd,
                                    text_parts=text_parts, reasoning_parts=reasoning_parts,
                                    allow_file_requests=allow_file_requests, on_chunk=on_chunk):
                        continue
                    if msg.get("id") != request_id:
                        continue
                    if "error" in msg:
                        raise RuntimeError(f"Warm ACP {method} failed: {(msg.get('error') or {}).get('message') or msg.get('error')}")
                    return msg.get("result")
                if proc.poll() is not None:
                    raise RuntimeError(f"Warm ACP process exited early (exit code {proc.returncode}).")
                raise TimeoutError(f"Timed out waiting for warm ACP response to {method}.")

            # initialize once per process (first checkout on this proc)
            if not slot.initialized:
                _request("initialize", _INITIALIZE_PARAMS)
                slot.initialized = True
            session = _request("session/new", {"cwd": tpl._acp_cwd, "mcpServers": []}) or {}
            session_id = str(session.get("sessionId") or "").strip()
            if not session_id:
                raise RuntimeError("Warm ACP did not return a sessionId.")
            if discovery:
                return _session_model_ids(session, tpl.model_entry_disabled)
            # model selection: identical semantics to base (degrade to default)
            requested_model = str(model or "").strip()
            # Hermes passes full ids like "mistral-vibe-acp/zai-glm-5-3"; the harness
            # advertises bare ids. Strip the provider prefix before matching.
            if "/" in requested_model:
                requested_model = requested_model.split("/", 1)[1]
            if requested_model and requested_model != tpl.placeholder_model:
                try:
                    selection = _model_selection_request(
                        session, requested_model, placeholder=tpl.placeholder_model, disabled=tpl.model_entry_disabled
                    )
                    if selection is not None:
                        _request(*selection)
                    else:
                        logger.warning("%s does not offer model %r; using the session default.", tpl.label, requested_model)
                except (RuntimeError, TimeoutError, OSError, TypeError, AttributeError) as exc:
                    logger.warning("%s model selection for %r failed; continuing with the session default: %s",
                                   tpl.label, requested_model, exc)
            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            prompt = {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt_text or ""}]}
            try:
                _request("session/prompt", prompt, text_parts=text_parts, reasoning_parts=reasoning_parts)
            finally:
                # reap the ACP session so the harness does not accumulate them
                with contextlib.suppress(Exception):
                    for method in ("session/cancel", "session/close"):
                        try:
                            body = {"sessionId": session_id} if method == "session/close" else {"sessionId": session_id}
                            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": next(request_ids), "method": method, "params": body}) + "\n")
                            proc.stdin.flush()
                            # don't wait for the response: fire and forget; a close reply
                            # may arrive interleaved and be ignored by the id mismatch
                            break  # only send the close request, not cancel
                        except OSError:
                            break
            return "".join(text_parts), "".join(reasoning_parts)
        except (RuntimeError, TimeoutError, OSError) as exc:
            healthy = not _is_poisonous(str(exc))
            raise
        finally:
            self._release_slot(slot, healthy=healthy)

    def _handle(self, msg, *, process, cwd: str, text_parts, reasoning_parts, allow_file_requests: bool,
                on_chunk=None) -> bool:
        """Delegate every server->client message to the base-class bridge verbatim.

        When ``on_chunk`` is set, live agent_message_chunk text is ALSO forwarded
        to the callback as it lands (real streaming), in addition to the normal
        buffering."""
        if on_chunk is not None and isinstance(msg, dict) and msg.get("method") == "session/update":
            update = (msg.get("params") or {}).get("update") or {}
            if str(update.get("sessionUpdate") or "") == "agent_message_chunk":
                content = update.get("content") or {}
                chunk_text = str(content.get("text") or "") if isinstance(content, dict) else ""
                if chunk_text:
                    try:
                        on_chunk(chunk_text)
                    except Exception:
                        pass
        tpl = self._template_cached
        return tpl._handle_server_message(
            msg, process=process, cwd=cwd, text_parts=text_parts, reasoning_parts=reasoning_parts,
            allow_file_requests=allow_file_requests,
        )

    @property
    def _template_cached(self):
        if not hasattr(self, "_tpl_cache") or self._tpl_cache is None:
            self._tpl_cache = self._template()
        return self._tpl_cache

    def _drive_streaming_session(self, *, timeout_seconds: float, model: str | None = None,
                                 prompt_text: str | None = None) -> tuple[str, str]:
        """Same turn as _drive_session, but yields live chunk callbacks through a
        blocking queue so the gateway's stream consumer sees text as it lands."""
        result_holder: dict[str, Any] = {}
        result_evt = threading.Event()

        def _on_chunk(chunk: str) -> None:
            result_holder.setdefault("chunks", []).append((time.monotonic(), chunk))

        def _work() -> None:
            try:
                text, reasoning = self._drive_session(
                    timeout_seconds=timeout_seconds, model=model, prompt_text=prompt_text,
                    on_chunk=_on_chunk,
                )
                result_holder["final"] = (text, reasoning)
            except BaseException as exc:  # surface every failure shape to the consumer
                result_holder["error"] = exc
            finally:
                result_evt.set()

        worker = threading.Thread(target=_work, daemon=True)
        self._stream_state = result_holder
        worker.start()

        # Emit chunks as they land (backed by a small poll loop); the outer
        # generator yields each live chunk, then the final assembled text.
        emitted = 0
        while not result_evt.is_set():
            chunks_now = result_holder.get("chunks") or []
            while emitted < len(chunks_now):
                _, chunk_text = chunks_now[emitted]
                emitted += 1
                yield chunk_text
            time.sleep(0.05)
        # drain any remaining chunks recorded before the event
        chunks_now = result_holder.get("chunks") or []
        while emitted < len(chunks_now):
            _, chunk_text = chunks_now[emitted]
            emitted += 1
            yield chunk_text
        if "error" in result_holder:
            raise result_holder["error"]
        text, reasoning = result_holder.get("final") or ("", "")
        # final text may exceed the sum of observed chunks (tool-call turns): the
        # gateway joins chunks itself, so hand back the authoritative full text.
        result_holder["final_text"], result_holder["final_reasoning"] = text, reasoning

    # -- OpenAI facade -------------------------------------------------------
    def _create_chat_completion(self, *, model: str | None = None, messages: list[dict[str, Any]] | None = None,
                                timeout: float | None = None, stream: bool = False, **__: Any) -> Any:
        from agent.acp_client import _effective_timeout, _extract_tool_calls_from_text, _completion_to_stream_chunks

        messages = messages or []
        prompt_text = _format_messages_as_prompt(messages, model=model)
        if stream:
            # Streaming facade: consume the live-chunk generator and yield
            # OpenAI-style stream chunks (this function itself returns an iterator).
            def _stream_facade():
                mid = model or self.placeholder_model
                emitted = 0
                stream_id = f"chatcmpl-acp-{time.time_ns()}"
                def _chunk(delta_text: str):
                    return {
                        "id": stream_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": mid,
                        "choices": [{"index": 0, "delta": {"role": "assistant", "content": delta_text}, "finish_reason": None}],
                    }
                gen = self._drive_streaming_session(
                    timeout_seconds=_effective_timeout(timeout), model=model, prompt_text=prompt_text,
                )
                for piece in gen:
                    if isinstance(piece, str) and piece:
                        yield _chunk(piece)
                    emitted += 1
                final_holder = getattr(self, "_stream_state", None) or {}
                text = final_holder.get("final_text") or ""
                tool_calls, cleaned_text = _extract_tool_calls_from_text(text)
                if tool_calls:
                    yield {
                        "id": stream_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": mid,
                        "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": tool_calls}, "finish_reason": None}],
                    }
                yield {
                    "id": stream_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": mid,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }

            return _stream_facade()
        text, reasoning = self._drive_session(
            timeout_seconds=_effective_timeout(timeout), model=model, prompt_text=prompt_text,
        )
        tool_calls, cleaned_text = _extract_tool_calls_from_text(text)
        from types import SimpleNamespace

        message = SimpleNamespace(
            content=cleaned_text, tool_calls=tool_calls, reasoning=reasoning or None,
            reasoning_content=reasoning or None, reasoning_details=None,
        )
        completion = SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="tool_calls" if tool_calls else "stop")],
            usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0,
                                  prompt_tokens_details=SimpleNamespace(cached_tokens=0)),
            model=model or "mistral-large-4-0",
        )
        return _completion_to_stream_chunks(completion) if stream else completion

    def list_models(self, *, timeout_seconds: float = 15.0) -> list[str]:
        """Model discovery over the warm process (no throwaway spawn)."""
        result = self._drive_session(timeout_seconds=timeout_seconds, discovery=True, allow_file_requests=False)
        return list(result or [])

    def close(self) -> None:
        with self._lock:
            for slot in list(self._slots):
                self._drop_slot_locked(slot)
            self._active_processes.clear()
            self.is_closed = True


def _format_messages_as_prompt(messages: list[dict[str, Any]], *, model: str | None = None) -> str:
    """Base-class exact formatter: preamble + transcript + continue line."""
    from agent.acp_client import _format_messages_as_prompt as _base_format

    return _base_format(messages, model=model)


class MistralVibeACPProfile(ProviderProfile):
    """Mistral Vibe ACP — warm external process over stdio, no REST models endpoint."""

    def create_client(self, **client_kwargs: Any) -> Any:
        kwargs = dict(client_kwargs)
        kwargs.setdefault("command", _COMMAND)
        kwargs.setdefault("args", list(_ARGS))
        client = WarmACPClient(
            command=_COMMAND,
            args=list(_ARGS),
            pool_size=1,
            api_key=kwargs.get("api_key") or "mistral-vibe-acp",
            base_url=kwargs.get("base_url") or "acp://mistral-vibe-acp",
        )
        client.marker_base_url = "acp://mistral-vibe-acp"
        client.placeholder_model = "mistral-large-4-0"
        client.label = "Mistral Vibe ACP"
        client.missing_command_hint = "Install mistral-vibe (uv tool install mistral-vibe) — vibe-acp must be on PATH."
        client.unsupported_transport_hint = "vibe-acp rejected the ACP transport; check the mistral-vibe install."
        return client

    def fetch_models(
        self, *, api_key: str | None = None, base_url: str | None = None, timeout: float = 30.0
    ) -> list[str] | None:
        """Models the harness advertises from the warm process."""
        try:
            client = self.create_client(api_key=api_key, base_url=base_url)
            return client.list_models(timeout_seconds=timeout) or None
        except Exception:
            return None


mistral_vibe_acp = MistralVibeACPProfile(
    name="mistral-vibe-acp",
    aliases=("mistral_vibe_acp", "vibe-acp"),
    api_mode="chat_completions",
    env_vars=(),
    base_url="acp://mistral-vibe-acp",
    display_name="Mistral Vibe ACP",
    description="Mistral Vibe harness over ACP — bills the Vibe allowance, not API credit",
    auth_type="external_process",
    process_command=_COMMAND,
    process_args=_ARGS,
    fallback_models=_FALLBACK_MODELS,
)


register_provider(mistral_vibe_acp)
