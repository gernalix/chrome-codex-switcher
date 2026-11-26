"""Use the system Public Suffix List, including private suffixes."""
from __future__ import annotations

import ctypes
import ipaddress
from functools import lru_cache
from urllib.parse import urlsplit


@lru_cache(maxsize=1)
def _psl():
    library = ctypes.CDLL("libpsl.so.5")
    library.psl_builtin.restype = ctypes.c_void_p
    library.psl_registrable_domain.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    library.psl_registrable_domain.restype = ctypes.c_char_p
    return library, library.psl_builtin()


@lru_cache(maxsize=4096)
def overlay_domain(url: str) -> str | None:
    """Return the registrable DNS name, or the exact IP/single-label host."""
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        host = parts.hostname.rstrip(".").lower()
        try:
            return ipaddress.ip_address(host).compressed
        except ValueError:
            pass
        host = host.encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        return None
    library, context = _psl()
    result = library.psl_registrable_domain(context, host.encode("ascii"))
    return result.decode("ascii") if result else host
