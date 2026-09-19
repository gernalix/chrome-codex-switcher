from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import time
from pathlib import Path
from typing import Callable, Any

from .util import overlay_path

PROMPT_ID_RE = re.compile(r"\d{6}\Z")
SESSION_ID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$",
    re.IGNORECASE,
)


def _read_overlay() -> dict[str, Any]:
    path = overlay_path()
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def discover_codex_session(
    prompt_id: str,
    *,
    session_root: Path | None = None,
    max_age_seconds: float = 6 * 3600,
) -> dict[str, str] | None:
    """Find one recent Codex session that explicitly contains this PROMPT_ID.

    Never chooses among multiple matches. The filename UUID is Codex's native
    session/thread identifier and is used by the existing codex://threads URI.
    """
    if not PROMPT_ID_RE.fullmatch(str(prompt_id)):
        raise ValueError("invalid_prompt_id")
    root = (session_root or Path("~/.codex/sessions")).expanduser()
    if not root.is_dir():
        return None
    cutoff = time.time() - max_age_seconds
    needle = re.compile(rf"PROMPT[_ ]ID\s*=\s*{re.escape(prompt_id)}\b", re.IGNORECASE)
    matches: list[tuple[float, Path, str]] = []
    for path in root.rglob("rollout-*.jsonl"):
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime < cutoff:
            continue
        match = SESSION_ID_RE.search(path.name)
        if not match:
            continue
        found = False
        read_bytes = 0
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    read_bytes += len(line.encode("utf-8", errors="ignore"))
                    if needle.search(line):
                        found = True
                        break
                    if read_bytes >= 4 * 1024 * 1024:
                        break
        except OSError:
            continue
        if found:
            matches.append((stat.st_mtime, path, match.group(1)))
    if len(matches) != 1:
        return None
    _mtime, path, session_id = matches[0]
    return {
        "session_id": session_id,
        "deep_link": f"codex://threads/{session_id}",
        "source_path": str(path),
    }



def _api_call(request: Callable[..., dict], path: str, payload: dict | None = None, timeout: float = 4.0) -> dict:
    return request(path, payload, timeout=timeout)


def _control_probe(
    prompt_id: str,
    *,
    request: Callable[..., dict],
    action: str = "probe",
    timeout: float = 8.0,
) -> dict[str, Any]:
    started = _api_call(
        request,
        "/api/prompt/control",
        {"prompt_id": prompt_id, "action": action},
    )
    if not started.get("ok"):
        return {"ok": False, "error": started.get("error") or "control_start_failed"}
    request_id = str(started.get("request_id") or "")
    if not request_id:
        return {"ok": False, "error": "control_request_id_missing"}
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = _api_call(request, f"/api/control/ack?request_id={request_id}")
        if last.get("ok") and isinstance(last.get("ack"), dict):
            return last["ack"]
        time.sleep(0.2)
    return {"ok": False, "error": "control_ack_timeout", "last": last}


def verify_binding(
    prompt_id: str,
    *,
    request: Callable[..., dict],
) -> dict[str, Any]:
    if not PROMPT_ID_RE.fullmatch(str(prompt_id)):
        raise ValueError("invalid_prompt_id")
    response = _api_call(request, f"/api/prompt?prompt_id={prompt_id}")
    binding = response.get("binding") if response.get("ok") else None
    if not isinstance(binding, dict):
        return {"prompt_id": prompt_id, "result": "BLOCKED", "error": "binding_missing"}
    context_id = str(binding.get("context_id") or "")
    thread = str(binding.get("codex_thread") or "")
    deep_link = str(binding.get("codex_deep_link") or "")
    if not context_id or not thread or not deep_link:
        return {
            "prompt_id": prompt_id,
            "result": "BLOCKED",
            "error": "binding_incomplete",
            "binding": binding,
        }
    context_response = _api_call(request, f"/api/context?context_id={context_id}")
    context = context_response.get("context") if context_response.get("ok") else None
    twin = context.get("twin") if isinstance(context, dict) else None
    consistent = bool(
        isinstance(context, dict)
        and isinstance(twin, dict)
        and twin.get("codex_thread") == thread
        and twin.get("codex_deep_link") == deep_link
    )
    return {
        "prompt_id": prompt_id,
        "result": "PASS" if consistent else "BLOCKED",
        "binding": binding,
        "context": context,
        "checks": {
            "context_id": bool(context_id),
            "codex_thread": bool(thread),
            "deep_link": bool(deep_link),
            "twin_consistent": consistent,
        },
        "error": None if consistent else "context_twin_mismatch",
    }


def verify_note(
    prompt_id: str,
    *,
    request: Callable[..., dict],
    timeout: float = 8.0,
) -> dict[str, Any]:
    binding = verify_binding(prompt_id, request=request)
    if binding.get("result") != "PASS":
        return {
            "prompt_id": prompt_id,
            "result": "BLOCKED",
            "error": binding.get("error"),
            "binding": binding,
        }
    context = binding["context"]
    independent = bool(context.get("notes_independent"))
    chrome_note = str(context.get("note") or "")
    codex_note = str(context.get("codex_note") or "")
    stored_consistent = independent or chrome_note == codex_note
    probe = _control_probe(prompt_id, request=request, action="probe", timeout=timeout)
    rendered = probe.get("rendered") if isinstance(probe, dict) else None
    chrome_rendered = bool(
        probe.get("ok")
        and isinstance(rendered, dict)
        and rendered.get("context_id") == context.get("id")
        and str(rendered.get("note") or "") == chrome_note
    )
    gnome_response = _api_call(request, "/api/gnome-runtime")
    gnome = gnome_response.get("gnome_runtime") if gnome_response.get("ok") else None
    gnome_check: bool | None = None
    if isinstance(gnome, dict) and gnome.get("visible") and gnome.get("context_id") == context.get("id"):
        expected = codex_note if independent else chrome_note
        gnome_check = str(gnome.get("note") or "") == expected
    passed = stored_consistent and chrome_rendered and gnome_check is not False
    return {
        "prompt_id": prompt_id,
        "result": "PASS" if passed else "BLOCKED",
        "mode": "split" if independent else "shared",
        "chrome_note_sha256": hashlib.sha256(chrome_note.encode("utf-8")).hexdigest(),
        "codex_note_sha256": hashlib.sha256(codex_note.encode("utf-8")).hexdigest(),
        "updated_at": context.get("updated_at"),
        "checks": {
            "stored_consistent": stored_consistent,
            "chrome_rendered": chrome_rendered,
            "gnome_rendered_if_active": gnome_check,
        },
        "error": None if passed else "note_state_mismatch",
    }


def verify_overlay(
    *,
    request: Callable[..., dict],
) -> dict[str, Any]:
    health = _api_call(request, "/api/health")
    overlay = _read_overlay()
    gnome_response = _api_call(request, "/api/gnome-runtime")
    gnome = gnome_response.get("gnome_runtime") if gnome_response.get("ok") else None
    a11y = bool(health.get("codex_a11y_watch") and not health.get("codex_a11y_error"))
    overlay_valid = True
    if overlay.get("visible"):
        overlay_valid = bool(overlay.get("context_id") and overlay.get("codex_thread"))
    gnome_consistent = True
    if isinstance(gnome, dict):
        if gnome.get("visible"):
            gnome_consistent = bool(
                overlay.get("visible")
                and gnome.get("context_id") == overlay.get("context_id")
                and gnome.get("codex_thread") == overlay.get("codex_thread")
            )
        elif not gnome.get("focused_codex"):
            gnome_consistent = not bool(gnome.get("visible"))
    passed = a11y and overlay_valid and gnome_consistent
    return {
        "result": "PASS" if passed else "BLOCKED",
        "checks": {
            "a11y": a11y,
            "overlay_valid": overlay_valid,
            "gnome_consistent": gnome_consistent,
        },
        "overlay": overlay,
        "gnome_runtime": gnome,
        "error": None if passed else "overlay_state_mismatch",
    }


def verify_workflowy(
    prompt_id: str,
    *,
    request: Callable[..., dict],
) -> dict[str, Any]:
    if not PROMPT_ID_RE.fullmatch(str(prompt_id)):
        raise ValueError("invalid_prompt_id")
    text = _api_call(request, f"/api/prompt/text?prompt_id={prompt_id}")
    binding_response = _api_call(request, f"/api/prompt?prompt_id={prompt_id}")
    binding = binding_response.get("binding") if binding_response.get("ok") else None
    health = _api_call(request, "/api/health")
    extension = health.get("extension_runtime") if isinstance(health, dict) else None
    extension_fresh = bool(
        isinstance(extension, dict)
        and extension.get("version")
        and time.time() - float(extension.get("seen_at") or 0) <= 90
    )
    actions = {
        "copy": bool(text.get("ok") and text.get("prompt_text")),
        "launch": extension_fresh,
        "chrome": bool(isinstance(binding, dict) and binding.get("context_id")),
        "codex": bool(isinstance(binding, dict) and binding.get("codex_deep_link")),
    }
    passed = all(actions.values())
    return {
        "prompt_id": prompt_id,
        "result": "PASS" if passed else "BLOCKED",
        "actions": actions,
        "error": None if passed else "workflowy_action_unavailable",
    }


def self_test(
    *,
    request: Callable[..., dict],
) -> dict[str, Any]:
    health = _api_call(request, "/api/health")
    extension = health.get("extension_runtime") if isinstance(health, dict) else None
    extension_fresh = bool(
        isinstance(extension, dict)
        and extension.get("version")
        and time.time() - float(extension.get("seen_at") or 0) <= 90
    )
    gnome_response = _api_call(request, "/api/gnome-runtime")
    gnome = gnome_response.get("gnome_runtime") if gnome_response.get("ok") else None
    gnome_fresh = bool(
        isinstance(gnome, dict)
        and time.time() - float(gnome.get("seen_at") or 0) <= 10
    )
    try:
        with socket.create_connection(("127.0.0.1", 8765), timeout=1.0):
            workflowy_bridge = True
    except OSError:
        workflowy_bridge = False
    contexts = _api_call(request, "/api/list")
    events = _api_call(request, "/api/events?after=0&timeout=0")
    bindings_response = _api_call(request, "/api/prompts")
    bindings = bindings_response.get("bindings") if bindings_response.get("ok") else []
    prompt_proxy = False
    if isinstance(bindings, list):
        prompt_id = next(
            (
                str(row.get("prompt_id"))
                for row in bindings
                if isinstance(row, dict) and PROMPT_ID_RE.fullmatch(str(row.get("prompt_id") or ""))
            ),
            None,
        )
        if prompt_id:
            proxy = _api_call(request, f"/api/prompt/text?prompt_id={prompt_id}")
            prompt_proxy = bool(proxy.get("ok") and proxy.get("prompt_text"))
    gates = {
        "daemon": bool(health.get("ok")),
        "extension_heartbeat": extension_fresh,
        "gnome_companion": gnome_fresh,
        "clipboard_backend": bool(
            health.get("xfixes_watch") or health.get("clipboard_watch") or gnome_fresh
        ),
        "atspi": bool(health.get("codex_a11y_watch") and not health.get("codex_a11y_error")),
        "localhost_api": bool(health.get("ok")),
        "sqlite": bool(contexts.get("ok")),
        "workflowy_bridge": workflowy_bridge,
        "prompt_proxy": prompt_proxy,
        "overlay_cache": overlay_path().exists(),
        "event_broker": bool(events.get("ok")),
    }
    passed = all(gates.values())
    return {
        "result": "PASS" if passed else "BLOCKED",
        "gates": gates,
        "error": None if passed else "self_test_gate_failed",
    }


def verify_prompt(
    prompt_id: str,
    *,
    request: Callable[..., dict],
    full: bool = False,
    timeout: float = 12.0,
    session_root: Path | None = None,
) -> dict[str, Any]:
    if not PROMPT_ID_RE.fullmatch(str(prompt_id)):
        raise ValueError("invalid_prompt_id")

    result: dict[str, Any] = {
        "prompt_id": prompt_id,
        "result": "BLOCKED",
        "gates": {},
        "repair": [],
    }
    gates: dict[str, Any] = result["gates"]

    def call(path: str, payload: dict | None = None, request_timeout: float = 4.0) -> dict:
        return request(path, payload, timeout=request_timeout)

    def wait_until(fn, predicate, label: str) -> Any:
        deadline = time.monotonic() + timeout
        last: Any = None
        while time.monotonic() < deadline:
            try:
                last = fn()
                if predicate(last):
                    return last
            except Exception as exc:  # surface the last concrete observation
                last = {"error": str(exc)}
            time.sleep(0.25)
        raise RuntimeError(f"{label}_timeout:{last}")

    def control(action: str) -> dict:
        started = call(
            "/api/prompt/control",
            {"prompt_id": prompt_id, "action": action},
        )
        if not started.get("ok"):
            raise RuntimeError(str(started.get("error") or "control_start_failed"))
        request_id = str(started.get("request_id") or "")
        if not request_id:
            raise RuntimeError("control_request_id_missing")
        return wait_until(
            lambda: call(f"/api/control/ack?request_id={request_id}"),
            lambda value: bool(value.get("ok") and isinstance(value.get("ack"), dict)),
            f"chrome_{action}_ack",
        )["ack"]

    health = call("/api/health")
    extension = health.get("extension_runtime") if isinstance(health, dict) else None
    extension_fresh = (
        isinstance(extension, dict)
        and bool(extension.get("version"))
        and time.time() - float(extension.get("seen_at") or 0) <= 90
    )
    gates["daemon"] = bool(health.get("ok"))
    gates["extension_runtime"] = {
        "pass": extension_fresh,
        "version": extension.get("version") if isinstance(extension, dict) else None,
    }
    gates["a11y"] = bool(
        health.get("codex_a11y_watch") and not health.get("codex_a11y_error")
    )
    if not all(
        (
            gates["daemon"],
            gates["extension_runtime"]["pass"],
            gates["a11y"],
        )
    ):
        result["blocker"] = "runtime_health_failed"
        return result

    binding_response = call(f"/api/prompt?prompt_id={prompt_id}")
    binding = binding_response.get("binding") if binding_response.get("ok") else None

    if not isinstance(binding, dict) or not binding.get("context_id"):
        ack = control("ensure")
        if not ack.get("ok"):
            result["blocker"] = f"chrome_context_ensure_failed:{ack.get('error')}"
            return result
        result["repair"].append("chrome_context")
        binding_response = call(f"/api/prompt?prompt_id={prompt_id}")
        binding = binding_response.get("binding") if binding_response.get("ok") else None

    if not isinstance(binding, dict) or not binding.get("context_id"):
        result["blocker"] = "chrome_context_missing"
        return result

    context_id = str(binding["context_id"])
    context_response = call(f"/api/context?context_id={context_id}")
    context = context_response.get("context") if context_response.get("ok") else None
    if not isinstance(context, dict):
        result["blocker"] = "chrome_context_not_found"
        return result

    if not binding.get("codex_thread") or not binding.get("codex_deep_link"):
        discovered = discover_codex_session(prompt_id, session_root=session_root)
        if not discovered:
            result["blocker"] = "codex_thread_missing_or_ambiguous"
            gates["session_discovery"] = False
            return result
        armed = call(
            "/api/prompt/arm",
            {
                "prompt_id": prompt_id,
                "context_id": context_id,
                "url": context.get("url") or binding.get("url") or "",
                "title": context.get("title") or binding.get("title") or "",
            },
        )
        if not armed.get("ok"):
            result["blocker"] = f"prompt_arm_failed:{armed.get('error')}"
            return result
        paired = call("/api/clipboard", {"text": discovered["deep_link"]})
        if not paired.get("ok"):
            result["blocker"] = f"session_pair_failed:{paired.get('error')}"
            return result
        result["repair"].append("codex_thread_from_native_session")
        gates["session_discovery"] = {
            "pass": True,
            "session_id": discovered["session_id"],
        }
        binding_response = call(f"/api/prompt?prompt_id={prompt_id}")
        binding = binding_response.get("binding") if binding_response.get("ok") else None

    complete = bool(
        isinstance(binding, dict)
        and binding.get("context_id")
        and binding.get("codex_thread")
        and binding.get("codex_deep_link")
    )
    gates["binding"] = complete
    if not complete:
        result["blocker"] = "prompt_binding_incomplete"
        return result

    thread = str(binding["codex_thread"])
    context_response = call(f"/api/context?context_id={context_id}")
    context = context_response.get("context") if context_response.get("ok") else None
    twin = context.get("twin") if isinstance(context, dict) else None
    gates["twin"] = bool(
        isinstance(twin, dict)
        and twin.get("codex_thread") == thread
        and twin.get("codex_deep_link") == binding.get("codex_deep_link")
    )
    if not gates["twin"]:
        result["blocker"] = "context_twin_mismatch"
        return result

    chrome_probe = control("probe")
    gates["chrome_probe"] = bool(
        chrome_probe.get("ok")
        and chrome_probe.get("context_id") == context_id
        and isinstance(chrome_probe.get("rendered"), dict)
        and chrome_probe["rendered"].get("context_id") == context_id
    )
    if not gates["chrome_probe"]:
        result["blocker"] = f"chrome_probe_failed:{chrome_probe.get('error')}"
        return result

    if not full:
        result["result"] = "PASS"
        result["blocker"] = None
        result["binding"] = binding
        return result

    original = {
        "note": str(context.get("note") or ""),
        "codex_note": str(context.get("codex_note") or ""),
        "independent": bool(context.get("notes_independent")),
    }

    def gnome_runtime() -> dict:
        value = call("/api/gnome-runtime")
        state = value.get("gnome_runtime")
        return state if isinstance(state, dict) else {}

    def wait_gnome_note(wanted: str) -> dict:
        return wait_until(
            gnome_runtime,
            lambda state: bool(
                state.get("visible")
                and state.get("context_id") == context_id
                and state.get("codex_thread") == thread
                and state.get("note") == wanted
                and time.time() - float(state.get("seen_at") or 0) <= 3
            ),
            "gnome_note",
        )

    def wait_chrome_note(wanted: str) -> dict:
        return wait_until(
            lambda: control("probe"),
            lambda ack: bool(
                ack.get("ok")
                and isinstance(ack.get("rendered"), dict)
                and ack["rendered"].get("context_id") == context_id
                and ack["rendered"].get("note") == wanted
            ),
            "chrome_note",
        )

    try:
        opened = call("/api/prompt/open-codex", {"prompt_id": prompt_id})
        if not opened.get("ok"):
            raise RuntimeError(f"open_codex_failed:{opened.get('error')}")
        visible = wait_until(
            gnome_runtime,
            lambda state: bool(
                state.get("focused_codex")
                and state.get("visible")
                and state.get("context_id") == context_id
                and state.get("codex_thread") == thread
                and time.time() - float(state.get("seen_at") or 0) <= 3
            ),
            "codex_overlay",
        )
        gates["chrome_to_codex"] = True
        gates["codex_overlay"] = {
            "pass": True,
            "context_id": visible.get("context_id"),
            "codex_thread": visible.get("codex_thread"),
        }

        if original["independent"]:
            merged = call(
                "/api/note-mode",
                {
                    "context_id": context_id,
                    "independent": False,
                    "source": "chrome",
                    "note": original["note"],
                },
            )
            if not merged.get("ok"):
                raise RuntimeError("temporary_shared_mode_failed")

        token = f"{int(time.time() * 1000)}-{os.getpid()}"
        chrome_marker = f"__ccs_verify_{prompt_id}_chrome_{token}__"
        codex_marker = f"__ccs_verify_{prompt_id}_codex_{token}__"

        changed = call(
            "/api/note",
            {"context_id": context_id, "note": chrome_marker, "surface": "chrome"},
        )
        if not changed.get("ok"):
            raise RuntimeError("chrome_note_write_failed")
        wait_chrome_note(chrome_marker)
        wait_gnome_note(chrome_marker)
        gates["note_chrome_to_codex"] = True

        changed = call(
            "/api/note",
            {"context_id": context_id, "note": codex_marker, "surface": "codex"},
        )
        if not changed.get("ok"):
            raise RuntimeError("codex_note_write_failed")
        wait_chrome_note(codex_marker)
        wait_gnome_note(codex_marker)
        gates["note_codex_to_chrome"] = True

        hidden = call("/api/codex-activity", {"kind": "verify-stale-guard"})
        if not hidden.get("ok"):
            raise RuntimeError("stale_guard_invalidation_failed")
        overlay = _read_overlay()
        if overlay.get("visible"):
            raise RuntimeError("stale_guard_did_not_hide")
        recovered = wait_until(
            gnome_runtime,
            lambda state: bool(
                state.get("visible")
                and state.get("context_id") == context_id
                and state.get("codex_thread") == thread
            ),
            "stale_guard_recovery",
        )
        gates["stale_note_guard"] = bool(recovered)

        chrome_focus = control("focus")
        gates["codex_to_chrome"] = bool(
            chrome_focus.get("ok")
            and chrome_focus.get("active")
            and chrome_focus.get("context_id") == context_id
        )
        if not gates["codex_to_chrome"]:
            raise RuntimeError(f"chrome_focus_failed:{chrome_focus.get('error')}")

    except Exception as exc:
        result["blocker"] = str(exc)
        return result
    finally:
        try:
            call(
                "/api/note",
                {"context_id": context_id, "note": original["note"], "surface": "chrome"},
            )
            if original["independent"]:
                call(
                    "/api/note-mode",
                    {
                        "context_id": context_id,
                        "independent": True,
                        "source": "chrome",
                        "note": original["note"],
                    },
                )
                call(
                    "/api/note",
                    {
                        "context_id": context_id,
                        "note": original["codex_note"],
                        "surface": "codex",
                    },
                )
            else:
                call(
                    "/api/note-mode",
                    {
                        "context_id": context_id,
                        "independent": False,
                        "source": "chrome",
                        "note": original["note"],
                    },
                )
        except Exception as restore_exc:
            gates["note_restore"] = {"pass": False, "error": str(restore_exc)}
        else:
            gates["note_restore"] = True

    required = (
        gates.get("daemon"),
        gates.get("extension_runtime", {}).get("pass"),
        gates.get("a11y"),
        gates.get("binding"),
        gates.get("twin"),
        gates.get("chrome_probe"),
        gates.get("chrome_to_codex"),
        gates.get("codex_overlay", {}).get("pass"),
        gates.get("note_chrome_to_codex"),
        gates.get("note_codex_to_chrome"),
        gates.get("stale_note_guard"),
        gates.get("codex_to_chrome"),
        gates.get("note_restore") is True,
    )
    result["result"] = "PASS" if all(required) else "BLOCKED"
    result["blocker"] = None if result["result"] == "PASS" else "one_or_more_gates_failed"
    result["binding"] = binding
    return result
