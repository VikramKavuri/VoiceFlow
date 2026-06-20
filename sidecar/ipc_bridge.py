"""
VoiceFlow Sidecar - IPC bridge between Tauri (parent) and the Python sidecar.

Communication protocol:
  - Parent  -> Sidecar:  one JSON object per line on **stdin**
  - Sidecar -> Parent:   one JSON object per line on **stdout** (flushed)
  - All diagnostic / log output goes to **stderr** so it never pollutes the
    structured IPC channel.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class IPCBridge:
    """Bidirectional JSON-lines IPC over stdin / stdout."""

    # ------------------------------------------------------------------
    # Reading commands from the parent process
    # ------------------------------------------------------------------

    @staticmethod
    def read_command() -> dict[str, Any] | None:
        """Read a single JSON line from *stdin* and return the parsed dict.

        Returns ``None`` when stdin is closed (parent exited).
        Malformed lines are logged and skipped by returning an empty dict
        with an ``"_error"`` key so callers can decide what to do.
        """
        try:
            line = sys.stdin.readline()
        except (EOFError, OSError):
            return None

        if not line:
            # stdin closed
            return None

        line = line.strip()
        if not line:
            return None

        try:
            cmd = json.loads(line)
            if not isinstance(cmd, dict):
                logger.warning("IPC: expected JSON object, got %s", type(cmd).__name__)
                return {"_error": "expected_object"}
            return cmd
        except json.JSONDecodeError as exc:
            logger.warning("IPC: malformed JSON (%s): %s", exc, line[:120])
            return {"_error": "malformed_json", "_raw": line[:120]}

    # ------------------------------------------------------------------
    # Sending events to the parent process
    # ------------------------------------------------------------------

    @staticmethod
    def send_event(event_type: str, **data: Any) -> None:
        """Write a JSON event line to *stdout* and flush immediately.

        Every event carries at minimum ``{"event": "<event_type>"}``.
        Additional keyword arguments are merged into the payload.
        """
        payload: dict[str, Any] = {"event": event_type, **data}
        try:
            line = json.dumps(payload, ensure_ascii=False, default=str)
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            # Parent is gone - nothing we can do.
            logger.error("IPC: broken pipe while sending event '%s'", event_type)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    @staticmethod
    def run(handler: Callable[[dict[str, Any]], None]) -> None:
        """Block on *stdin*, dispatching each parsed command to *handler*.

        The loop exits cleanly when stdin closes or the handler raises
        ``SystemExit``.
        """
        logger.info("IPC bridge: entering main loop")
        try:
            while True:
                cmd = IPCBridge.read_command()
                if cmd is None:
                    logger.info("IPC bridge: stdin closed, exiting loop")
                    break
                if "_error" in cmd:
                    IPCBridge.send_event("error", message=f"Bad command: {cmd.get('_error')}")
                    continue
                try:
                    handler(cmd)
                except SystemExit:
                    raise
                except Exception:
                    logger.exception("IPC bridge: unhandled error in handler")
                    IPCBridge.send_event("error", message="Internal handler error")
        except SystemExit:
            logger.info("IPC bridge: handler requested shutdown")
        except KeyboardInterrupt:
            logger.info("IPC bridge: interrupted")
        finally:
            logger.info("IPC bridge: main loop ended")
