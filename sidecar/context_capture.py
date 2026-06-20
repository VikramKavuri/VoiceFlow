"""Best-effort active target context for transcript formatting.

The app can always capture the target window title/class from TextInjector.
When comtypes/UIAutomation is available, this module also tries to read the
focused control value and current text selection without changing focus or
touching the clipboard.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ActiveTextContext:
    window_title: str = ""
    window_class: str = ""
    control_name: str = ""
    control_class: str = ""
    surrounding_text: str = ""
    selected_text: str = ""

    def compact(self, max_chars: int = 1200) -> str:
        parts: list[str] = []
        if self.window_title or self.window_class:
            parts.append(f"Target window: {self.window_title or 'unknown'} ({self.window_class or 'unknown'})")
        if self.control_name or self.control_class:
            parts.append(f"Focused control: {self.control_name or 'unknown'} ({self.control_class or 'unknown'})")
        if self.surrounding_text:
            text = self.surrounding_text.strip()
            if len(text) > max_chars:
                half = max_chars // 2
                text = text[:half] + "\n...\n" + text[-half:]
            parts.append(f"Existing text near cursor:\n{text}")
        if self.selected_text:
            parts.append(f"Selected text:\n{self.selected_text[:max_chars]}")
        return "\n".join(parts)


def capture_active_text_context(target_info: object | None = None) -> ActiveTextContext:
    context = ActiveTextContext()

    target_hwnd = 0
    if target_info is not None:
        context.window_title = str(getattr(target_info, "title", "") or "")
        context.window_class = str(getattr(target_info, "class_name", "") or "")
        try:
            target_hwnd = int(getattr(target_info, "window_handle", 0) or 0)
        except (TypeError, ValueError):
            target_hwnd = 0

    try:
        _augment_with_uia(context, target_hwnd)
    except Exception:
        logger.debug("UIAutomation context capture failed", exc_info=True)

    return context


def _augment_with_uia(context: ActiveTextContext, target_hwnd: int = 0) -> None:
    """Walk the UIAutomation tree under *target_hwnd* and harvest text content.

    Always anchor on the saved target HWND (not GetFocusedElement) because by
    the time we run, focus has already moved to the VoiceFlow recording
    overlay. Looking up the focused element returns our own window's "View"
    control, which carries no useful text.
    """
    try:
        import comtypes.client  # type: ignore
    except Exception:
        return

    try:
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA  # type: ignore
        automation = comtypes.client.CreateObject(
            UIA.CUIAutomation,
            interface=UIA.IUIAutomation,
        )

        root = None
        if target_hwnd:
            try:
                root = automation.ElementFromHandle(target_hwnd)
            except Exception:
                root = None

        # Fall back to the focused element if no target HWND was given (e.g.
        # debug/manual invocations).
        if root is None:
            try:
                root = automation.GetFocusedElement()
            except Exception:
                root = None
        if root is None:
            return

        # Try the anchor element first
        _harvest_text_from_element(root, UIA, context)
        if context.surrounding_text and context.control_class:
            return

        # If nothing useful came back, walk descendants looking for an editable
        # text element. Most rich-text targets (Notepad, VS Code, browser
        # editors, Slack) expose a TextPattern-supporting descendant even
        # when the window root itself does not.
        try:
            candidate = _find_text_descendant(automation, root, UIA)
            if candidate is not None:
                _harvest_text_from_element(candidate, UIA, context)
        except Exception:
            logger.debug("UIA descendant walk failed", exc_info=True)
    except Exception:
        logger.debug("UIAutomation read failed", exc_info=True)


def _harvest_text_from_element(element: object, uia: object, context: ActiveTextContext) -> None:
    """Populate context fields from a single element. Safe on partial failures."""
    if not context.control_name:
        context.control_name = _get_property(element, uia.UIA_NamePropertyId)
    if not context.control_class:
        context.control_class = _get_property(element, uia.UIA_ClassNamePropertyId)

    if not context.surrounding_text:
        value = _read_value_pattern(element, uia)
        if value:
            context.surrounding_text = value

    selected, document = _read_text_pattern(element, uia)
    if selected and not context.selected_text:
        context.selected_text = selected
    if document and not context.surrounding_text:
        context.surrounding_text = document


def _find_text_descendant(automation: object, root: object, uia: object):
    """Return a descendant of *root* that supports TextPattern or ValuePattern.

    Prefers elements with non-empty content. Capped at the first ~30 descendants
    to keep latency well under 50ms even on heavy windows.
    """
    try:
        # IsTextPatternAvailable OR IsValuePatternAvailable — covers Notepad,
        # WordPad, browsers, IDEs, chat apps, Notepad++, etc.
        text_avail = automation.CreatePropertyCondition(
            uia.UIA_IsTextPatternAvailablePropertyId, True
        )
        value_avail = automation.CreatePropertyCondition(
            uia.UIA_IsValuePatternAvailablePropertyId, True
        )
        cond = automation.CreateOrCondition(text_avail, value_avail)
        # TreeScope_Descendants = 4
        matches = root.FindAll(4, cond)
        if not matches or not matches.Length:
            return None

        best = None
        best_len = -1
        for i in range(min(matches.Length, 30)):
            el = matches.GetElement(i)
            if el is None:
                continue
            # Prefer elements with actual content
            v = _read_value_pattern(el, uia)
            _, d = _read_text_pattern(el, uia)
            length = max(len(v or ""), len(d or ""))
            if length > best_len:
                best_len = length
                best = el
        return best
    except Exception:
        logger.debug("FindAll text descendants failed", exc_info=True)
        return None


def _get_property(element: object, prop_id: int) -> str:
    try:
        value = element.GetCurrentPropertyValue(prop_id)
        return "" if value is None else str(value)
    except Exception:
        return ""


def _read_value_pattern(element: object, uia: object) -> str:
    try:
        pattern = element.GetCurrentPattern(uia.UIA_ValuePatternId)
        value_pattern = pattern.QueryInterface(uia.IUIAutomationValuePattern)
        return str(value_pattern.CurrentValue or "")
    except Exception:
        return ""


def _read_text_pattern(element: object, uia: object) -> tuple[str, str]:
    selected = ""
    document = ""
    try:
        pattern = element.GetCurrentPattern(uia.UIA_TextPatternId)
        text_pattern = pattern.QueryInterface(uia.IUIAutomationTextPattern)

        try:
            selection = text_pattern.GetSelection()
            if selection and selection.Length:
                selected = str(selection.GetElement(0).GetText(800) or "")
        except Exception:
            selected = ""

        try:
            document_range = text_pattern.DocumentRange
            document = str(document_range.GetText(8000) or "")
        except Exception:
            document = ""
    except Exception:
        pass
    return selected, document
