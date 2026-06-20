"""
VoiceFlow Sidecar - Text Injector v2

Integrates VoiceVault's multi-method injection architecture with VoiceFlow's
UIAutomation focus tracking, password-field detection, and HIPAA compliance.

Injection method priority (auto-selected per app category):
    1. SENDINPUT   — Win32 SendInput Unicode (native apps, best latency)
    2. CLIPBOARD   — Save → Ctrl+V → restore  (default fallback)
    3. TERM_PASTE  — Ctrl+Shift+V  (terminal emulators)
    4. PYNPUT      — char-by-char via pynput  (RDP / last resort)

Backward-compatible API for main.py:
    inject(text)            → bool  (sync, used for live streaming)
    inject_async(text)      → None  (queued, non-blocking)
    capture_target()        → Optional[TargetInfo]
    copy_to_clipboard(text) → bool
    clear_target()          → None
    stop()                  → None  (flush queue, print stats)

New API:
    inject_sync(text)       → InjectionResult
    stats                   → InjectorStats  (property)
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import io
import contextlib
import logging
import platform
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------
IS_WINDOWS = platform.system() == "Windows"

# ---------------------------------------------------------------------------
# Optional imports — fail gracefully
# ---------------------------------------------------------------------------
try:
    from pynput.keyboard import Controller as _PynputKeyboard, Key as _PynputKey
    HAS_PYNPUT = True
except ImportError:
    HAS_PYNPUT = False

try:
    import pyperclip
    HAS_PYPERCLIP = True
except ImportError:
    HAS_PYPERCLIP = False

# pywin32 — used for app classification (get_foreground_process)
try:
    import win32gui
    import win32process
    import win32api
    import win32con
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

# ---------------------------------------------------------------------------
# Win32 constants
# ---------------------------------------------------------------------------
CF_UNICODETEXT: int = 13
KEYEVENTF_KEYUP: int = 0x0002
KEYEVENTF_UNICODE: int = 0x0004
INPUT_KEYBOARD: int = 1
VK_CONTROL: int = 0x11
VK_MENU: int = 0x12
VK_SHIFT: int = 0x10
VK_V: int = 0x56
GMEM_MOVEABLE: int = 0x0002
ES_PASSWORD: int = 0x0020
GWL_STYLE: int = -16
WM_PASTE: int = 0x0302

_BROWSER_WINDOW_CLASSES: frozenset[str] = frozenset({
    "Chrome_WidgetWin_1",
    "MozillaWindowClass",
    "ApplicationFrameWindow",
})
_DIRECT_PASTE_CLASSES: frozenset[str] = frozenset({
    "Edit",
    "RichEditD2DPT",
    "RichEdit20W",
    "RICHEDIT50W",
})

# ---------------------------------------------------------------------------
# Fix ctypes pointer truncation on 64-bit Windows
# ---------------------------------------------------------------------------
_user32   = ctypes.windll.user32    # type: ignore[attr-defined]
_kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

_kernel32.GlobalAlloc.argtypes  = [ctypes.wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype   = ctypes.c_void_p
_kernel32.GlobalLock.argtypes   = [ctypes.c_void_p]
_kernel32.GlobalLock.restype    = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalUnlock.restype  = ctypes.wintypes.BOOL
_kernel32.GlobalFree.argtypes   = [ctypes.c_void_p]
_kernel32.GlobalFree.restype    = ctypes.c_void_p

_user32.OpenClipboard.argtypes    = [ctypes.wintypes.HWND]
_user32.OpenClipboard.restype     = ctypes.wintypes.BOOL
_user32.CloseClipboard.argtypes   = []
_user32.CloseClipboard.restype    = ctypes.wintypes.BOOL
_user32.EmptyClipboard.argtypes   = []
_user32.EmptyClipboard.restype    = ctypes.wintypes.BOOL
_user32.SetClipboardData.argtypes = [ctypes.wintypes.UINT, ctypes.c_void_p]
_user32.SetClipboardData.restype  = ctypes.c_void_p
_user32.GetClipboardData.argtypes = [ctypes.wintypes.UINT]
_user32.GetClipboardData.restype  = ctypes.c_void_p
_user32.GetDesktopWindow.restype  = ctypes.wintypes.HWND
_user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
_user32.GetFocus.restype          = ctypes.wintypes.HWND
_user32.SetFocus.argtypes         = [ctypes.wintypes.HWND]
_user32.SetFocus.restype          = ctypes.wintypes.HWND
_user32.SetForegroundWindow.argtypes = [ctypes.wintypes.HWND]
_user32.SetForegroundWindow.restype  = ctypes.wintypes.BOOL
_user32.SetActiveWindow.argtypes  = [ctypes.wintypes.HWND]
_user32.SetActiveWindow.restype   = ctypes.wintypes.HWND
_user32.AttachThreadInput.argtypes = [
    ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.wintypes.BOOL,
]
_user32.AttachThreadInput.restype  = ctypes.wintypes.BOOL
_user32.GetWindowThreadProcessId.argtypes = [ctypes.wintypes.HWND, ctypes.c_void_p]
_user32.GetWindowThreadProcessId.restype  = ctypes.wintypes.DWORD
_user32.IsWindow.argtypes         = [ctypes.wintypes.HWND]
_user32.IsWindow.restype          = ctypes.wintypes.BOOL
_user32.GetWindowLongW.argtypes   = [ctypes.wintypes.HWND, ctypes.c_int]
_user32.GetWindowLongW.restype    = ctypes.c_long
_user32.SendMessageW.argtypes     = [
    ctypes.wintypes.HWND,
    ctypes.wintypes.UINT,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM,
]
_user32.SendMessageW.restype      = ctypes.wintypes.LPARAM
_kernel32.GetCurrentThreadId.restype = ctypes.wintypes.DWORD


# ---------------------------------------------------------------------------
# Win32 INPUT structures (for SendInput)
# ---------------------------------------------------------------------------

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk",         ctypes.wintypes.WORD),
        ("wScan",       ctypes.wintypes.WORD),
        ("dwFlags",     ctypes.wintypes.DWORD),
        ("time",        ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class INPUT(ctypes.Structure):
    class _INPUT_UNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT)]
    _anonymous_ = ("_union",)
    _fields_ = [
        ("type",   ctypes.wintypes.DWORD),
        ("_union", _INPUT_UNION),
    ]


# ---------------------------------------------------------------------------
# Data types (v2 additions)
# ---------------------------------------------------------------------------

class InjectionMethod(Enum):
    SENDINPUT  = auto()   # Win32 SendInput Unicode (best for native apps)
    CLIPBOARD  = auto()   # Ctrl+V via clipboard
    TERM_PASTE = auto()   # Ctrl+Shift+V (terminal emulators)
    PYNPUT     = auto()   # pynput char-by-char (RDP / last resort)


class AppCategory(Enum):
    NATIVE_WIN32 = auto()   # Notepad, Word, etc.
    ELECTRON     = auto()   # VS Code, Slack, Discord, Cursor
    BROWSER      = auto()   # Chrome, Firefox, Edge
    TERMINAL     = auto()   # Windows Terminal, cmd, PowerShell, WSL
    OFFICE       = auto()   # Microsoft Office suite
    RDP_BLOCKED  = auto()   # Remote Desktop without clipboard share
    UNKNOWN      = auto()


@dataclass
class InjectionResult:
    success:    bool
    method:     InjectionMethod
    latency_ms: float
    app_name:   str
    category:   AppCategory
    char_count: int
    error:      Optional[str] = None


@dataclass
class FinalDeliveryResult:
    copied_to_clipboard: bool
    pasted_to_target: bool
    manual_paste_required: bool
    status: str
    failure_reason: Optional[str] = None


@dataclass
class InjectorStats:
    total:          int   = 0
    successful:     int   = 0
    failed:         int   = 0
    fallback_count: int   = 0
    total_chars:    int   = 0
    _latencies:     list  = field(default_factory=list)

    @property
    def avg_latency_ms(self) -> float:
        return sum(self._latencies) / len(self._latencies) if self._latencies else 0.0

    @property
    def p95_latency_ms(self) -> float:
        if not self._latencies:
            return 0.0
        sl = sorted(self._latencies)
        idx = max(0, int(len(sl) * 0.95) - 1)
        return sl[idx]

    def record(self, result: InjectionResult) -> None:
        self.total      += 1
        self.total_chars += result.char_count
        if result.success:
            self.successful += 1
            self._latencies.append(result.latency_ms)
        else:
            self.failed += 1
        if result.method not in (InjectionMethod.SENDINPUT, InjectionMethod.CLIPBOARD):
            self.fallback_count += 1

    def report(self) -> str:
        return (
            f"\n{'═' * 52}\n"
            f"  TextInjector v2 — Session Stats\n"
            f"{'─' * 52}\n"
            f"  Total injections : {self.total}\n"
            f"  Successful       : {self.successful}\n"
            f"  Failed           : {self.failed}\n"
            f"  Fallback used    : {self.fallback_count}×\n"
            f"  Avg latency      : {self.avg_latency_ms:.1f} ms\n"
            f"  P95 latency      : {self.p95_latency_ms:.1f} ms\n"
            f"  Total chars out  : {self.total_chars}\n"
            f"{'═' * 52}\n"
        )


# ---------------------------------------------------------------------------
# App classification (v2 addition, uses pywin32 if available)
# ---------------------------------------------------------------------------

_TERMINAL_PROCS: frozenset[str] = frozenset({
    "windowsterminal", "cmd", "powershell", "pwsh", "wt",
    "bash", "wsl", "conhost", "alacritty", "hyper", "terminus",
})
_ELECTRON_PROCS: frozenset[str] = frozenset({
    "code", "slack", "discord", "notion", "obsidian",
    "cursor", "figma", "postman", "teams",
})
_BROWSER_PROCS: frozenset[str] = frozenset({
    "chrome", "firefox", "msedge", "opera", "brave", "vivaldi",
})
_OFFICE_PROCS: frozenset[str] = frozenset({
    "winword", "excel", "powerpnt", "onenote", "outlook", "mspub",
})


def _classify_process(exe_name: str) -> AppCategory:
    name = exe_name.lower().replace(".exe", "").strip()
    if name in _TERMINAL_PROCS:  return AppCategory.TERMINAL
    if name in _ELECTRON_PROCS:  return AppCategory.ELECTRON
    if name in _BROWSER_PROCS:   return AppCategory.BROWSER
    if name in _OFFICE_PROCS:    return AppCategory.OFFICE
    return AppCategory.NATIVE_WIN32


def _get_foreground_process() -> tuple[str, int, AppCategory]:
    """Return (exe_name, hwnd, category). Falls back gracefully."""
    if not HAS_WIN32:
        return ("unknown", 0, AppCategory.UNKNOWN)
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return ("unknown", 0, AppCategory.UNKNOWN)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ, False, pid
        )
        exe_path = win32process.GetModuleFileNameEx(handle, 0)
        exe_name = exe_path.split("\\")[-1]
        win32api.CloseHandle(handle)
        return (exe_name, hwnd, _classify_process(exe_name))
    except Exception:
        return ("unknown", 0, AppCategory.UNKNOWN)


# ---------------------------------------------------------------------------
# TargetInfo — carries the window/control to restore focus to
# ---------------------------------------------------------------------------

@dataclass
class WindowInfo:
    title: str
    class_name: str


@dataclass
class TargetInfo:
    window_handle:  int
    control_handle: Optional[int]
    title:          str
    class_name:     str


# ---------------------------------------------------------------------------
# FocusTracker — polls the foreground window 10× per second and remembers
# the last window that was NOT a VoiceFlow window.  This solves the race
# condition where capture_target() runs AFTER the hotkey has already given
# focus to the Tauri app/overlay, causing text to be injected into the wrong
# window.
# ---------------------------------------------------------------------------

class FocusTracker:
    """Background thread that continuously records the last real user window.

    VoiceFlow's own windows (overlay, settings) are ignored so that
    capture_target() always gets the user's actual cursor destination.
    """

    _POLL_S = 0.1   # 100 ms poll interval
    # Window titles and class names that belong to VoiceFlow itself
    _OWN_TITLES: frozenset[str] = frozenset({
        "VoiceFlow Overlay",
        "VoiceFlow Transcriptor",
        "VoiceFlow Settings",
    })
    _OWN_CLASSES: frozenset[str] = frozenset({
        "Tauri Window",
    })

    def __init__(self) -> None:
        self._hwnd:       int = 0
        self._title:      str = ""
        self._class_name: str = ""
        self._lock        = threading.Lock()
        self._thread      = threading.Thread(
            target=self._poll_loop, name="FocusTracker", daemon=True
        )
        self._thread.start()

    def _is_own_window(self, title: str, class_name: str) -> bool:
        if title in self._OWN_TITLES or class_name in self._OWN_CLASSES:
            return True
        if "VoiceFlow" in title:
            return True
        return False

    def _poll_loop(self) -> None:
        while True:
            try:
                hwnd = _user32.GetForegroundWindow()
                if hwnd:
                    # Cheap check: only describe if hwnd changed
                    with self._lock:
                        last = self._hwnd
                    if hwnd != last:
                        try:
                            title, class_name = _describe_window_static(hwnd)
                        except Exception:
                            title, class_name = "", ""
                        if not self._is_own_window(title, class_name):
                            with self._lock:
                                self._hwnd       = int(hwnd)
                                self._title      = title
                                self._class_name = class_name
            except Exception:
                pass
            time.sleep(self._POLL_S)

    def last(self) -> tuple[int, str, str]:
        """Return (hwnd, title, class_name) of the last non-VoiceFlow window."""
        with self._lock:
            return self._hwnd, self._title, self._class_name


def _describe_window_static(hwnd: int) -> tuple[str, str]:
    """Module-level helper so FocusTracker can call it before TextInjector exists."""
    length  = _user32.GetWindowTextLengthW(hwnd)
    buf     = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    cls_buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, cls_buf, 256)
    return buf.value, cls_buf.value


# Module-level singleton — starts tracking as soon as the module is imported
_focus_tracker = FocusTracker()


# ---------------------------------------------------------------------------
# TextInjector — main class
# ---------------------------------------------------------------------------

class TextInjector:
    """
    Multi-method text injector with app classification, session stats,
    UIAutomation browser support, password-field protection, and
    HIPAA-compliant clipboard handling.

    Backward-compatible with VoiceFlow main.py (v1 API preserved).
    """

    _FOCUS_RESTORE_DELAY_S    = 0.030
    _CAPTURE_DELAY_S          = 0.080   # let OS restore focus after hotkey
    _SENDINPUT_CHAR_DELAY_S   = 0.005
    _PYNPUT_CHAR_DELAY_S      = 0.008
    _QUEUE_TIMEOUT_S          = 5.0

    def __init__(self) -> None:
        self._target: Optional[TargetInfo] = None
        self._stats = InjectorStats()
        self._lock  = threading.Lock()

        # Async queue (for inject_async / worker thread)
        self._pending: queue.Queue[Optional[str]] = queue.Queue()
        self._running = True
        self._worker  = threading.Thread(
            target=self._worker_loop, name="TextInjectorWorker", daemon=True
        )
        self._worker.start()

        # pynput keyboard (lazy)
        self._pynput_kb = None
        if HAS_PYNPUT:
            try:
                from pynput.keyboard import Controller as KC
                self._pynput_kb = KC()
            except Exception:
                pass

        logger.info(
            "TextInjector v2 ready (pynput=%s, pywin32=%s, pyperclip=%s)",
            HAS_PYNPUT, HAS_WIN32, HAS_PYPERCLIP,
        )

    # ------------------------------------------------------------------
    # Backward-compatible public API (v1)
    # ------------------------------------------------------------------

    def capture_target(self) -> Optional[TargetInfo]:
        """Capture the user's window for this recording session.

        Uses the FocusTracker's last known non-VoiceFlow window rather than
        calling GetForegroundWindow() here — by the time this runs, the
        hotkey has already transferred focus to the Tauri process/overlay,
        so GetForegroundWindow() would return the wrong window.
        """
        hwnd, title, class_name = _focus_tracker.last()

        # Validate — window must still exist
        if not hwnd or not _user32.IsWindow(hwnd):
            # Fallback: wait for OS to settle and try directly
            time.sleep(self._CAPTURE_DELAY_S)
            hwnd = _user32.GetForegroundWindow()
            if not hwnd:
                self._target = None
                logger.warning("capture_target: no foreground window found")
                return None
            title, class_name = self._describe_window(hwnd)

        focused = self._get_focused_control(hwnd)
        self._target = TargetInfo(
            window_handle=int(hwnd),
            control_handle=int(focused) if focused else None,
            title=title,
            class_name=class_name,
        )
        logger.info(
            "Captured target  window=%r  class=%s  control=%s",
            title, class_name, self._target.control_handle,
        )
        return self._target

    def clear_target(self) -> None:
        self._target = None

    def get_target_info(self) -> Optional[TargetInfo]:
        return self._target

    def inject(self, text: str) -> bool:
        """Synchronous injection — used by main.py for live streaming output.

        Returns True on success, False on failure.
        Records result in InjectorStats.
        """
        if not text:
            return False
        result = self._do_inject(text)
        with self._lock:
            self._stats.record(result)
        if result.success:
            logger.info(
                "INJECT [%s→%s] %.0fms  %r",
                result.category.name, result.method.name,
                result.latency_ms, text[:60],
            )
        else:
            logger.warning(
                "inject() FAILED [%s→%s] %r: %s",
                result.category.name, result.method.name,
                text[:40], result.error,
            )
        return result.success

    def copy_to_clipboard(self, text: str) -> bool:
        """Copy text to the system clipboard (no paste, no focus change).

        Retries up to 5 times with back-off if the clipboard is locked.
        Explicit buffer zeroing after encode (HIPAA).
        Returns True on success.
        """
        if not text:
            logger.warning("copy_to_clipboard called with empty text")
            return False

        logger.info("Copying %d chars to clipboard…", len(text))

        for attempt in range(5):
            try:
                hwnd = _user32.GetDesktopWindow()
                if not _user32.OpenClipboard(hwnd):
                    err = ctypes.get_last_error()
                    logger.warning(
                        "OpenClipboard failed (attempt %d/5, err=%d)", attempt + 1, err
                    )
                    time.sleep(0.15 * (attempt + 1))
                    continue
                try:
                    _user32.EmptyClipboard()
                    encoded = text.encode("utf-16-le") + b"\x00\x00"
                    h_mem = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
                    if not h_mem:
                        logger.error("GlobalAlloc failed")
                        continue
                    ptr = _kernel32.GlobalLock(h_mem)
                    if not ptr:
                        _kernel32.GlobalFree(h_mem)
                        logger.error("GlobalLock failed")
                        continue
                    ctypes.memmove(ptr, encoded, len(encoded))
                    _kernel32.GlobalUnlock(h_mem)
                    _user32.SetClipboardData(CF_UNICODETEXT, h_mem)
                    logger.info("Clipboard set (attempt %d)", attempt + 1)
                    return True
                finally:
                    _user32.CloseClipboard()
            except Exception:
                logger.exception("Clipboard error (attempt %d/5)", attempt + 1)
                time.sleep(0.15 * (attempt + 1))

        # Last resort: clip.exe
        try:
            import subprocess
            proc = subprocess.Popen(
                ["clip.exe"], stdin=subprocess.PIPE, creationflags=0x08000000
            )
            proc.communicate(input=text.encode("utf-16-le"))
            if proc.returncode == 0:
                logger.info("Clipboard set via clip.exe fallback")
                return True
        except Exception:
            logger.exception("clip.exe fallback also failed")

        logger.error("copy_to_clipboard failed after all attempts")
        return False

    def deliver_final_text(self, text: str) -> FinalDeliveryResult:
        """Copy the final text to the clipboard and paste it once into the target."""
        clean_text = text.strip()
        if not clean_text:
            return FinalDeliveryResult(
                copied_to_clipboard=False,
                pasted_to_target=False,
                manual_paste_required=False,
                status="copy_failed",
                failure_reason="empty_text",
            )

        copied = self.copy_to_clipboard(clean_text)
        if not copied:
            return FinalDeliveryResult(
                copied_to_clipboard=False,
                pasted_to_target=False,
                manual_paste_required=False,
                status="copy_failed",
                failure_reason="copy_failed",
            )

        pasted = self.paste_from_clipboard()
        if pasted:
            return FinalDeliveryResult(
                copied_to_clipboard=True,
                pasted_to_target=True,
                manual_paste_required=False,
                status="paste_succeeded",
            )

        return FinalDeliveryResult(
            copied_to_clipboard=True,
            pasted_to_target=False,
            manual_paste_required=True,
            status="paste_failed_but_copied",
            failure_reason="paste_failed",
        )

    def paste_from_clipboard(self) -> bool:
        """Restore focus to the captured target and paste the clipboard contents.

        The caller is responsible for placing the desired text on the clipboard
        (e.g. via copy_to_clipboard) before calling this method.  This method
        does NOT modify the clipboard — it only simulates the paste keystroke.

        Returns True if the paste keystroke was sent successfully.

        Behaviour:
          - Terminals (AppCategory.TERMINAL) receive Ctrl+Shift+V
          - All other apps receive Ctrl+V
        """
        focus_restored = self._restore_target_focus()
        if self._try_window_message_paste():
            return True
        if not focus_restored:
            logger.warning("paste_from_clipboard: could not restore target focus")
            return False

        if self._is_password_field():
            logger.warning("paste_from_clipboard: password field detected — skipped")
            return False

        _, _, category = _get_foreground_process()

        try:
            if category == AppCategory.TERMINAL:
                # Ctrl+Shift+V — standard terminal paste shortcut
                inputs = (INPUT * 6)(
                    INPUT(type=INPUT_KEYBOARD,
                          ki=KEYBDINPUT(wVk=VK_CONTROL, wScan=0, dwFlags=0, time=0,
                                        dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)))),
                    INPUT(type=INPUT_KEYBOARD,
                          ki=KEYBDINPUT(wVk=VK_SHIFT, wScan=0, dwFlags=0, time=0,
                                        dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)))),
                    INPUT(type=INPUT_KEYBOARD,
                          ki=KEYBDINPUT(wVk=VK_V, wScan=0, dwFlags=0, time=0,
                                        dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)))),
                    INPUT(type=INPUT_KEYBOARD,
                          ki=KEYBDINPUT(wVk=VK_V, wScan=0, dwFlags=KEYEVENTF_KEYUP, time=0,
                                        dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)))),
                    INPUT(type=INPUT_KEYBOARD,
                          ki=KEYBDINPUT(wVk=VK_SHIFT, wScan=0, dwFlags=KEYEVENTF_KEYUP, time=0,
                                        dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)))),
                    INPUT(type=INPUT_KEYBOARD,
                          ki=KEYBDINPUT(wVk=VK_CONTROL, wScan=0, dwFlags=KEYEVENTF_KEYUP, time=0,
                                        dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)))),
                )
                _user32.SendInput(6, ctypes.byref(inputs), ctypes.sizeof(INPUT))
                logger.info("paste_from_clipboard: sent Ctrl+Shift+V (terminal)")
            else:
                # Ctrl+V for everything else
                self._send_key_combo(VK_CONTROL, VK_V)
                logger.info(
                    "paste_from_clipboard: sent Ctrl+V (category=%s)", category.name
                )
            time.sleep(0.050)
            return True
        except Exception:
            logger.exception("paste_from_clipboard: keystroke error")
            return False

    # ------------------------------------------------------------------
    # New public API (v2)
    # ------------------------------------------------------------------

    def inject_async(self, text: str) -> None:
        """Queue text for background injection (non-blocking).

        Useful for high-frequency streaming calls where the caller cannot
        afford to block. The worker thread serialises injections.
        """
        if not text or not text.strip():
            return
        if not text.endswith((" ", "\n")):
            text += " "
        self._pending.put(text)

    def inject_sync(self, text: str) -> InjectionResult:
        """Synchronous injection returning a full InjectionResult."""
        result = self._do_inject(text)
        with self._lock:
            self._stats.record(result)
        return result

    def stop(self) -> None:
        """Flush the async queue, stop the worker, and log session stats."""
        self._running = False
        self._pending.put(None)   # sentinel
        self._worker.join(timeout=3.0)
        logger.info(self._stats.report())

    @property
    def stats(self) -> InjectorStats:
        return self._stats

    @staticmethod
    def get_active_window_info() -> Optional[WindowInfo]:
        try:
            hwnd = _user32.GetForegroundWindow()
            if not hwnd:
                return None
            title, class_name = TextInjector._describe_window(hwnd)
            return WindowInfo(title=title, class_name=class_name)
        except Exception:
            logger.exception("Failed to get active window info")
            return None

    # ------------------------------------------------------------------
    # Worker thread (for inject_async)
    # ------------------------------------------------------------------

    def _worker_loop(self) -> None:
        while self._running:
            try:
                chunk = self._pending.get(timeout=self._QUEUE_TIMEOUT_S)
            except queue.Empty:
                continue
            if chunk is None:
                break
            result = self._do_inject(chunk)
            with self._lock:
                self._stats.record(result)
            status = "✓" if result.success else "✗"
            logger.debug(
                "async %s [%s|%s] %.0f ms  %r",
                status, result.method.name, result.category.name,
                result.latency_ms, chunk[:40],
            )

    # ------------------------------------------------------------------
    # Core injection dispatcher
    # ------------------------------------------------------------------

    def _do_inject(self, text: str) -> InjectionResult:
        """Classify the target app, restore focus, choose method, inject."""
        t0 = time.perf_counter()

        if not self._restore_target_focus():
            return InjectionResult(
                success=False, method=InjectionMethod.SENDINPUT,
                latency_ms=0.0, app_name="unknown",
                category=AppCategory.UNKNOWN, char_count=len(text),
                error="Could not restore target focus",
            )

        if self._is_password_field():
            logger.warning("Password field detected — injection skipped")
            return InjectionResult(
                success=False, method=InjectionMethod.SENDINPUT,
                latency_ms=0.0, app_name="unknown",
                category=AppCategory.UNKNOWN, char_count=len(text),
                error="password_field",
            )

        exe_name, _, category = _get_foreground_process()
        method   = self._choose_method(category)
        success  = False
        error    = None

        try:
            if method == InjectionMethod.SENDINPUT:
                success = self._sendinput_inject(text)
            elif method == InjectionMethod.TERM_PASTE:
                success = self._term_paste_inject(text)
            elif method == InjectionMethod.PYNPUT:
                success = self._pynput_inject(text)
            else:  # CLIPBOARD
                success = self._clipboard_inject(text)
        except Exception as exc:
            error   = str(exc)
            success = False
            logger.exception("Injection error via %s", method.name)

        # Auto-fallback: if chosen method failed, try clipboard
        if not success and method != InjectionMethod.CLIPBOARD:
            logger.info(
                "Method %s failed, falling back to CLIPBOARD", method.name
            )
            try:
                success = self._clipboard_inject(text)
                if success:
                    method = InjectionMethod.CLIPBOARD
                    error  = None
            except Exception as exc2:
                error = str(exc2)

        latency_ms = (time.perf_counter() - t0) * 1000.0
        return InjectionResult(
            success=success, method=method, latency_ms=round(latency_ms, 2),
            app_name=exe_name, category=category, char_count=len(text),
            error=error,
        )

    def _choose_method(self, category: AppCategory) -> InjectionMethod:
        """Auto-select the best injection method for the detected app category.

        NATIVE_WIN32 / OFFICE / UNKNOWN  → SENDINPUT  (fastest, cursor-accurate)
        BROWSER / ELECTRON               → CLIPBOARD  (DOM focus, SendInput unreliable)
        TERMINAL                         → TERM_PASTE (Ctrl+Shift+V)
        RDP_BLOCKED                      → PYNPUT     (clipboard share often disabled)
        """
        if category == AppCategory.TERMINAL:
            return InjectionMethod.TERM_PASTE
        if category == AppCategory.RDP_BLOCKED:
            return InjectionMethod.PYNPUT if HAS_PYNPUT else InjectionMethod.CLIPBOARD
        if category in (AppCategory.BROWSER, AppCategory.ELECTRON):
            return InjectionMethod.CLIPBOARD
        # NATIVE_WIN32, OFFICE, UNKNOWN
        return InjectionMethod.SENDINPUT

    # ------------------------------------------------------------------
    # Injection methods
    # ------------------------------------------------------------------

    def _sendinput_inject(self, text: str) -> bool:
        """Type text character-by-character via Win32 SendInput Unicode."""
        for ch in text:
            code = ord(ch)
            inp_down = INPUT(
                type=INPUT_KEYBOARD,
                ki=KEYBDINPUT(
                    wVk=0, wScan=code, dwFlags=KEYEVENTF_UNICODE, time=0,
                    dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)),
                ),
            )
            inp_up = INPUT(
                type=INPUT_KEYBOARD,
                ki=KEYBDINPUT(
                    wVk=0, wScan=code,
                    dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, time=0,
                    dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)),
                ),
            )
            inputs = (INPUT * 2)(inp_down, inp_up)
            _user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(INPUT))
            time.sleep(self._SENDINPUT_CHAR_DELAY_S)
        logger.debug("SendInput: injected %d chars", len(text))
        return True

    def _clipboard_inject(self, text: str) -> bool:
        """Save clipboard → set text → Ctrl+V → restore clipboard."""
        saved = self._get_clipboard_text()
        try:
            self._set_clipboard_text(text)
            time.sleep(0.020)
            self._send_key_combo(VK_CONTROL, VK_V)
            time.sleep(0.050)
        finally:
            if saved is not None:
                self._set_clipboard_text(saved)
            else:
                self._clear_clipboard()
        logger.debug("Clipboard: injected %d chars", len(text))
        return True

    def _term_paste_inject(self, text: str) -> bool:
        """Ctrl+Shift+V paste — standard shortcut in terminal emulators."""
        self._set_clipboard_text(text)
        time.sleep(0.020)
        inputs = (INPUT * 6)(
            INPUT(type=INPUT_KEYBOARD,
                  ki=KEYBDINPUT(wVk=VK_CONTROL, wScan=0, dwFlags=0, time=0,
                                dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)))),
            INPUT(type=INPUT_KEYBOARD,
                  ki=KEYBDINPUT(wVk=VK_SHIFT, wScan=0, dwFlags=0, time=0,
                                dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)))),
            INPUT(type=INPUT_KEYBOARD,
                  ki=KEYBDINPUT(wVk=VK_V, wScan=0, dwFlags=0, time=0,
                                dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)))),
            INPUT(type=INPUT_KEYBOARD,
                  ki=KEYBDINPUT(wVk=VK_V, wScan=0, dwFlags=KEYEVENTF_KEYUP, time=0,
                                dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)))),
            INPUT(type=INPUT_KEYBOARD,
                  ki=KEYBDINPUT(wVk=VK_SHIFT, wScan=0, dwFlags=KEYEVENTF_KEYUP, time=0,
                                dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)))),
            INPUT(type=INPUT_KEYBOARD,
                  ki=KEYBDINPUT(wVk=VK_CONTROL, wScan=0, dwFlags=KEYEVENTF_KEYUP, time=0,
                                dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)))),
        )
        _user32.SendInput(6, ctypes.byref(inputs), ctypes.sizeof(INPUT))
        time.sleep(0.050)
        self._clear_clipboard()
        logger.debug("TermPaste: injected %d chars", len(text))
        return True

    def _pynput_inject(self, text: str) -> bool:
        """Character-by-character typing via pynput (RDP / last resort)."""
        if not HAS_PYNPUT or self._pynput_kb is None:
            logger.warning("pynput not available, falling back to clipboard")
            return self._clipboard_inject(text)
        for ch in text:
            self._pynput_kb.press(ch)
            self._pynput_kb.release(ch)
            time.sleep(self._PYNPUT_CHAR_DELAY_S)
        logger.debug("pynput: injected %d chars", len(text))
        return True

    # ------------------------------------------------------------------
    # Focus management (ported from v1, UIAutomation + Win32)
    # ------------------------------------------------------------------

    def _restore_target_focus(self) -> bool:
        if self._target is None:
            self.capture_target()
            return self._target is not None

        hwnd = self._target.window_handle
        if not hwnd or not _user32.IsWindow(hwnd):
            logger.warning("Stored target window is no longer valid")
            self.clear_target()
            return False

        restored = False
        for attempt in range(3):
            self._activate_window(hwnd, use_alt_hack=attempt > 0)
            time.sleep(self._FOCUS_RESTORE_DELAY_S * (attempt + 1))
            foreground = _user32.GetForegroundWindow()
            if foreground == hwnd:
                restored = True
                break

        if not self._is_browser_window():
            control = self._target.control_handle
            if control and _user32.IsWindow(control):
                target_thread = _user32.GetWindowThreadProcessId(hwnd, None)
                cur_thread    = _kernel32.GetCurrentThreadId()
                attached      = False
                try:
                    if target_thread and target_thread != cur_thread:
                        attached = bool(
                            _user32.AttachThreadInput(cur_thread, target_thread, True)
                        )
                    _user32.SetFocus(control)
                finally:
                    if attached:
                        _user32.AttachThreadInput(cur_thread, target_thread, False)
        return restored

    def _activate_window(self, hwnd: int, *, use_alt_hack: bool = False) -> None:
        if HAS_WIN32:
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.BringWindowToTop(hwnd)
            except Exception:
                logger.debug("ShowWindow/BringWindowToTop failed", exc_info=True)

        foreground = _user32.GetForegroundWindow()
        target_thread = _user32.GetWindowThreadProcessId(hwnd, None)
        foreground_thread = _user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        current_thread = _kernel32.GetCurrentThreadId()
        attached_pairs: list[tuple[int, int]] = []

        try:
            for first, second in (
                (current_thread, target_thread),
                (current_thread, foreground_thread),
                (foreground_thread, target_thread),
            ):
                if first and second and first != second:
                    if _user32.AttachThreadInput(first, second, True):
                        attached_pairs.append((first, second))

            if use_alt_hack:
                self._tap_key(VK_MENU)
            _user32.SetForegroundWindow(hwnd)
            _user32.SetActiveWindow(hwnd)
        finally:
            for first, second in reversed(attached_pairs):
                _user32.AttachThreadInput(first, second, False)

    def _try_window_message_paste(self) -> bool:
        if self._target is None:
            return False

        handles = [self._target.control_handle, self._target.window_handle]
        for handle in handles:
            if not handle or not _user32.IsWindow(handle):
                continue
            try:
                _, class_name = self._describe_window(handle)
            except Exception:
                continue
            if class_name not in _DIRECT_PASTE_CLASSES:
                continue
            try:
                _user32.SendMessageW(handle, WM_PASTE, 0, 0)
                logger.info("paste_from_clipboard: sent WM_PASTE to %s", class_name)
                time.sleep(0.030)
                return True
            except Exception:
                logger.exception("paste_from_clipboard: WM_PASTE failed")
        return False

    def _is_browser_window(self) -> bool:
        return (
            self._target is not None
            and self._target.class_name in _BROWSER_WINDOW_CLASSES
        )

    def _is_password_field(self) -> bool:
        try:
            hwnd    = self._target.window_handle if self._target else _user32.GetForegroundWindow()
            focused = self._get_focused_control(hwnd)
            if not focused and self._target and self._target.control_handle:
                focused = self._target.control_handle
            if not focused:
                return False
            style = _user32.GetWindowLongW(focused, GWL_STYLE)
            return bool(style & ES_PASSWORD)
        except Exception:
            logger.exception("Error checking password field")
            return False

    @staticmethod
    def _uia_get_focused_hwnd() -> Optional[int]:
        """UIAutomation — finds focus inside browser/Electron canvases."""
        try:
            import comtypes.client
            with contextlib.redirect_stdout(io.StringIO()):
                comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as UIA  # type: ignore[import]
            uia = comtypes.client.CreateObject(
                UIA.CUIAutomation, interface=UIA.IUIAutomation
            )
            el = uia.GetFocusedElement()
            if el is None:
                return None
            hwnd = el.CurrentNativeWindowHandle
            return int(hwnd) if hwnd else None
        except Exception:
            logger.debug("UIAutomation GetFocusedElement failed", exc_info=True)
            return None

    @staticmethod
    def _get_focused_control(hwnd: int) -> Optional[int]:
        if not hwnd:
            return None
        uia_hwnd = TextInjector._uia_get_focused_hwnd()
        if uia_hwnd:
            return uia_hwnd
        focused = _user32.GetFocus()
        if focused:
            return int(focused)
        fg_thread  = _user32.GetWindowThreadProcessId(hwnd, None)
        cur_thread = _kernel32.GetCurrentThreadId()
        attached   = False
        try:
            if fg_thread and fg_thread != cur_thread:
                attached = bool(_user32.AttachThreadInput(cur_thread, fg_thread, True))
            focused = _user32.GetFocus()
            return int(focused) if focused else None
        finally:
            if attached:
                _user32.AttachThreadInput(cur_thread, fg_thread, False)

    # ------------------------------------------------------------------
    # Clipboard helpers (HIPAA — explicit zeroing)
    # ------------------------------------------------------------------

    @staticmethod
    def _get_clipboard_text() -> Optional[str]:
        try:
            hwnd = _user32.GetDesktopWindow()
            if not _user32.OpenClipboard(hwnd):
                return None
            try:
                handle = _user32.GetClipboardData(CF_UNICODETEXT)
                if not handle:
                    return None
                ptr = _kernel32.GlobalLock(handle)
                if not ptr:
                    return None
                try:
                    return ctypes.wstring_at(ptr)  # type: ignore[attr-defined]
                finally:
                    _kernel32.GlobalUnlock(handle)
            finally:
                _user32.CloseClipboard()
        except Exception:
            logger.exception("Failed to read clipboard")
            return None

    @staticmethod
    def _set_clipboard_text(text: str) -> None:
        try:
            hwnd = _user32.GetDesktopWindow()
            if not _user32.OpenClipboard(hwnd):
                return
            try:
                _user32.EmptyClipboard()
                encoded = text.encode("utf-16-le") + b"\x00\x00"
                h_mem = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
                if not h_mem:
                    return
                ptr = _kernel32.GlobalLock(h_mem)
                if not ptr:
                    _kernel32.GlobalFree(h_mem)
                    return
                ctypes.memmove(ptr, encoded, len(encoded))
                _kernel32.GlobalUnlock(h_mem)
                _user32.SetClipboardData(CF_UNICODETEXT, h_mem)
            finally:
                _user32.CloseClipboard()
        except Exception:
            logger.exception("Failed to set clipboard")

    @staticmethod
    def _clear_clipboard() -> None:
        try:
            hwnd = _user32.GetDesktopWindow()
            if _user32.OpenClipboard(hwnd):
                _user32.EmptyClipboard()
                _user32.CloseClipboard()
        except Exception:
            logger.exception("Failed to clear clipboard")

    @staticmethod
    def _send_key_combo(modifier: int, key: int) -> None:
        inputs = (INPUT * 4)(
            INPUT(type=INPUT_KEYBOARD,
                  ki=KEYBDINPUT(wVk=modifier, wScan=0, dwFlags=0, time=0,
                                dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)))),
            INPUT(type=INPUT_KEYBOARD,
                  ki=KEYBDINPUT(wVk=key, wScan=0, dwFlags=0, time=0,
                                dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)))),
            INPUT(type=INPUT_KEYBOARD,
                  ki=KEYBDINPUT(wVk=key, wScan=0, dwFlags=KEYEVENTF_KEYUP, time=0,
                                dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)))),
            INPUT(type=INPUT_KEYBOARD,
                  ki=KEYBDINPUT(wVk=modifier, wScan=0, dwFlags=KEYEVENTF_KEYUP, time=0,
                                dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)))),
        )
        _user32.SendInput(4, ctypes.byref(inputs), ctypes.sizeof(INPUT))

    @staticmethod
    def _tap_key(key: int) -> None:
        inputs = (INPUT * 2)(
            INPUT(type=INPUT_KEYBOARD,
                  ki=KEYBDINPUT(wVk=key, wScan=0, dwFlags=0, time=0,
                                dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)))),
            INPUT(type=INPUT_KEYBOARD,
                  ki=KEYBDINPUT(wVk=key, wScan=0, dwFlags=KEYEVENTF_KEYUP, time=0,
                                dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)))),
        )
        _user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(INPUT))

    @staticmethod
    def _describe_window(hwnd: int) -> tuple[str, str]:
        length  = _user32.GetWindowTextLengthW(hwnd)
        buf     = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        cls_buf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, cls_buf, 256)
        return buf.value, cls_buf.value
