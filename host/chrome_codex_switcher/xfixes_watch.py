from __future__ import annotations

import ctypes
import threading
from collections.abc import Callable


class _SelectionNotify(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("subtype", ctypes.c_int),
        ("owner", ctypes.c_ulong),
        ("selection", ctypes.c_ulong),
        ("timestamp", ctypes.c_ulong),
        ("selection_timestamp", ctypes.c_ulong),
    ]


class _XEvent(ctypes.Union):
    _fields_ = [("type", ctypes.c_int), ("selection", _SelectionNotify), ("pad", ctypes.c_long * 24)]


class XFixesWatch:
    """Observe clipboard ownership changes through the running XWayland server."""

    def __init__(self, on_change: Callable[[], None]):
        self._on_change = on_change
        self._display: int | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        try:
            self._x11 = ctypes.CDLL("libX11.so.6")
            self._fixes = ctypes.CDLL("libXfixes.so.3")
            self._x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
            self._x11.XOpenDisplay.restype = ctypes.c_void_p
            self._x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
            self._x11.XDefaultRootWindow.restype = ctypes.c_ulong
            self._x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
            self._x11.XInternAtom.restype = ctypes.c_ulong
            self._x11.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.POINTER(_XEvent)]
            self._x11.XFlush.argtypes = [ctypes.c_void_p]
            self._fixes.XFixesQueryExtension.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
            self._fixes.XFixesQueryExtension.restype = ctypes.c_int
            self._fixes.XFixesSelectSelectionInput.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
            self._display = self._x11.XOpenDisplay(None)
            if not self._display:
                return False
            event_base = ctypes.c_int()
            error_base = ctypes.c_int()
            if not self._fixes.XFixesQueryExtension(self._display, ctypes.byref(event_base), ctypes.byref(error_base)):
                return False
            root = self._x11.XDefaultRootWindow(self._display)
            clipboard = self._x11.XInternAtom(self._display, b"CLIPBOARD", 0)
            self._fixes.XFixesSelectSelectionInput(self._display, root, clipboard, 1)
            self._x11.XFlush(self._display)
            self._event_type = event_base.value
        except (OSError, AttributeError):
            return False
        self._thread = threading.Thread(target=self._run, name="xfixes-clipboard", daemon=True)
        self._thread.start()
        return True

    def _run(self) -> None:
        event = _XEvent()
        while True:
            self._x11.XNextEvent(self._display, ctypes.byref(event))
            if event.type == self._event_type and event.selection.subtype == 0:
                self._on_change()

    @property
    def active(self) -> bool:
        return bool(self._thread and self._thread.is_alive())
