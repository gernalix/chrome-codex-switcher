from __future__ import annotations

import json
import os
import re
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


def verify_prompt(
    prompt_id: str,
    *,
    request: Callable[..., dict],
    full: bool = False,
    timeout: float = 12.0,
    session_root: Path | None = None,
    scope: str = "prompt",
) -> dict[str, Any]:
    if not PROMPT_ID_RE.fullmatch(str(prompt_id)):
        raise ValueError("invalid_prompt_id")
    if scope not in {"prompt", "binding", "note", "overlay", "workflowy"}:
        raise ValueError("invalid_verify_scope")

    result: dict[str, Any] = {
        "prompt_id": prompt_id,
        "scope": scope,
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
            gates["a11y"] or scope in {"binding", "workflowy"},
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

    if scope == "workflowy":
        workflowy = control("workflowy")
        gates["workflowy"] = bool(workflowy.get("ok") and workflowy.get("prompt_id") == prompt_id and workflowy.get("action_present"))
        result["result"] = "PASS" if gates["workflowy"] else "BLOCKED"
        result["blocker"] = None if gates["workflowy"] else f"workflowy_action_missing:{workflowy.get('error')}"
        return result

    if scope == "binding" or (not full and scope == "prompt"):
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

    failure = None
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

        if scope == "overlay":
            # The overlay can be checked without touching either note.
            chrome_focus = control("focus")
            gates["codex_to_chrome"] = bool(chrome_focus.get("ok") and chrome_focus.get("active") and chrome_focus.get("context_id") == context_id)
            if not gates["codex_to_chrome"]:
                raise RuntimeError(f"chrome_focus_failed:{chrome_focus.get('error')}")
            result["result"] = "PASS"
            result["blocker"] = None
            return result

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
        failure = str(exc)
    finally:
        if scope != "overlay":
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
            restored = call(f"/api/context?context_id={context_id}").get("context")
            gates["note_restore"] = bool(
                isinstance(restored, dict)
                and restored.get("note") == original["note"]
                and restored.get("codex_note") == original["codex_note"]
                and bool(restored.get("notes_independent")) == original["independent"]
            )
          except Exception as restore_exc:
            gates["note_restore"] = {"pass": False, "error": str(restore_exc)}

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
    result["result"] = "PASS" if all(required) and failure is None else "BLOCKED"
    result["blocker"] = None if result["result"] == "PASS" else (failure or "one_or_more_gates_failed")
    result["binding"] = binding
    return result
