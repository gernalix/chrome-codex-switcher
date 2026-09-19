from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from .util import overlay_path

PORT = int(os.environ.get("CCS_PORT", "43817"))
BASE = f"http://127.0.0.1:{PORT}"


def request(path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = Request(
        BASE + path,
        data=data,
        method="POST" if payload is not None else "GET",
        headers={"Content-Type": "application/json"} if payload is not None else {},
    )
    with urlopen(req, timeout=4) as response:
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
    result["clipboard_backend"] = "wl-paste" if wl_watch else ("gnome-shell" if gnome_bridge else None)
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
    args = parser.parse_args()
    try:
        raise SystemExit(args.func(args))
    except URLError as exc:
        print(f"daemon unavailable: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
