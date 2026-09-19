from __future__ import annotations

import json
import os
import sys
from urllib.request import Request, urlopen


def main() -> int:
    text = sys.stdin.read()
    if not text:
        return 0
    port = int(os.environ.get("CCS_PORT", "43817"))
    body = json.dumps({"text": text}).encode("utf-8")
    request = Request(
        f"http://127.0.0.1:{port}/api/clipboard",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=2) as response:
            response.read()
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
