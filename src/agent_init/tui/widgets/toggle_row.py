"""Custom focusable toggle row that works around Textual Checkbox issues.

Some terminals / mouse drivers do not reliably deliver events to Textual's
native Checkbox. This widget renders a simple `[ ]` / `[✕]` row, accepts
Space or Enter to toggle, and always toggles on click.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widgets import Static


class ToggleRow(Static, can_focus=True):
    """Focusable row with a clickable/keyboard toggle marker."""

    value: reactive[bool] = reactive(False)

    def __init__(
        self,
        label: str,
        *,
        value: bool = False,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._label = label
        self.value = value

    def _render_text(self) -> str:
        if self.value:
            return f"[b #ffb000][✕][/]  {self._label}"
        return f"[ ]  {self._label}"

    def compose(self) -> ComposeResult:
        yield from ()

    def watch_value(self) -> None:
        self.update(self._render_text())
        self.set_class(self.value, "-checked")

    def toggle(self) -> None:
        self.value = not self.value

    def on_click(self) -> None:
        self.toggle()

    def on_key(self, event) -> None:
        if event.key in ("space", "enter"):
            self.toggle()
            event.stop()
