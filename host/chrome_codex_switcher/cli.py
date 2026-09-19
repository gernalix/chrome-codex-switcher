from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from .util import overlay_path
from .verifier import verify_prompt, verify_overlay

PORT = int(os.environ.get("CCS_PORT", "43817"))
BASE = f"http://127.0.0.1:{PORT}"


def request(path: str, payload: dict | None = None, *, timeout: float = 4.0) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = Request(
        BASE + path,
        data=data,
        method="POST" if payload is not None else "GET",
        headers={"Content-Type": "application/json"} if payload is not None else {},
    )
    with urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def cmd_status(_args: argparse.Namespace) -> int:
    result: dict[str, object] = {}
    try:
        result["daemon"] = request("/api/health")
    except Exception as exc:
        result["daemon"] = {"ok": False, "error": str(exc)}

    try:
        mime = subprocess.run(
            ["xdg-mime", "query", "default", "x-scheme-handler/codex"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()
    except Exception:
        mime = ""
    result["codex_handler"] = mime or None
    result["wl_paste"] = shutil.which("wl-paste")
    result["overlay_cache"] = str(overlay_path())

    gnome_bridge = False
    if shutil.which("gnome-extensions"):
        try:
            enabled = subprocess.run(
                ["gnome-extensions", "list", "--enabled"],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout.splitlines()
            gnome_bridge = "chrome-codex-switcher@gernalix.github.com" in enabled
        except Exception:
            gnome_bridge = False
    result["gnome_shell_bridge"] = gnome_bridge

    daemon_ok = bool(isinstance(result["daemon"], dict) and result["daemon"].get("ok"))
    wl_watch = bool(isinstance(result["daemon"], dict) and result["daemon"].get("clipboard_watch"))
    xfixes_watch = bool(isinstance(result["daemon"], dict) and result["daemon"].get("xfixes_watch"))
    result["clipboard_backend"] = "xfixes" if xfixes_watch else ("wl-paste" if wl_watch else ("gnome-shell" if gnome_bridge else None))
    result["ok"] = bool(daemon_ok and mime and result["clipboard_backend"])
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["ok"] else 1


def cmd_list(_args: argparse.Namespace) -> int:
    print(json.dumps(request("/api/list"), indent=2, ensure_ascii=False))
    return 0


def cmd_capture(_args: argparse.Namespace) -> int:
    if not shutil.which("wl-paste"):
        print("wl-paste missing; install package wl-clipboard", file=sys.stderr)
        return 2
    proc = subprocess.run(["wl-paste", "--no-newline", "--type", "text"], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        print(proc.stderr.strip() or "clipboard read failed", file=sys.stderr)
        return proc.returncode or 1
    print(json.dumps(request("/api/clipboard", {"text": proc.stdout}), indent=2, ensure_ascii=False))
    return 0


def cmd_overlay_state(_args: argparse.Namespace) -> int:
    path = overlay_path()
    if not path.exists():
        print("{}")
        return 0
    print(path.read_text(encoding="utf-8"))
    return 0


def cmd_auto_switch(args: argparse.Namespace) -> int:
    result = request("/api/settings", {"auto_switch_on_codex_copy": args.value == "on"})
    print(json.dumps(result, indent=2))
    return 0


def cmd_verify_prompt(args: argparse.Namespace) -> int:
    result = verify_prompt(
        args.prompt_id,
        request=request,
        full=args.full,
        timeout=args.timeout,
        session_root=args.session_root,
        scope=args.scope,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("result") == "PASS" else 2


def cmd_verify_overlay(_args: argparse.Namespace) -> int:
    result = verify_overlay(request=request)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["result"] == "PASS" else 2


def cmd_self_test(_args: argparse.Namespace) -> int:
    try:
        health = request("/api/health")
        extension = health.get("extension_runtime") or {}
        gnome = health.get("gnome_runtime") or {}
        gates = {
            "daemon": bool(health.get("ok")),
            "extension_runtime": bool(extension.get("version") and time.time() - float(extension.get("seen_at") or 0) <= 90),
            "gnome_companion": bool(time.time() - float(gnome.get("seen_at") or 0) <= 3),
            "a11y": bool(health.get("codex_a11y_watch") and not health.get("codex_a11y_error")),
        }
    except Exception as exc:
        gates = {"daemon": False}
        error = str(exc)
    else:
        error = None
    result = {"result": "PASS" if all(gates.values()) else "BLOCKED", "gates": gates, "blocker": error or (None if all(gates.values()) else "runtime_health_failed")}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["result"] == "PASS" else 2


def main() -> None:
    parser = argparse.ArgumentParser(prog="context-twin")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("list").set_defaults(func=cmd_list)
    sub.add_parser("capture-clipboard").set_defaults(func=cmd_capture)
    sub.add_parser("overlay-state").set_defaults(func=cmd_overlay_state)
    auto = sub.add_parser("auto-switch")
    auto.add_argument("value", choices=["on", "off"])
    auto.set_defaults(func=cmd_auto_switch)

    verify = sub.add_parser(
        "verify-prompt",
        help="Programmatically verify one roadmap prompt's Chrome/Codex runtime binding",
    )
    verify.add_argument("prompt_id")
    verify.add_argument("--full", action="store_true", help="Exercise navigation, overlay and shared-note propagation with automatic restore")
    verify.add_argument("--timeout", type=float, default=12.0)
    verify.add_argument("--session-root", type=Path, default=Path("~/.codex/sessions"))
    verify.set_defaults(scope="prompt")
    verify.set_defaults(func=cmd_verify_prompt)
    for name, scope in (("verify-binding", "binding"), ("verify-note", "note"), ("verify-workflowy", "workflowy")):
        command = sub.add_parser(name)
        command.add_argument("prompt_id")
        command.add_argument("--timeout", type=float, default=12.0)
        command.set_defaults(func=cmd_verify_prompt, scope=scope, full=scope == "note", session_root=Path("~/.codex/sessions"))
    sub.add_parser("verify-overlay").set_defaults(func=cmd_verify_overlay)
    sub.add_parser("self-test").set_defaults(func=cmd_self_test)
    args = parser.parse_args()
    try:
        raise SystemExit(args.func(args))
    except URLError as exc:
        print(f"daemon unavailable: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
