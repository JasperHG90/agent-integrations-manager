"""Repo-filter picker — pick a single repo to filter an artifact browser by,
or "All repos" to clear the filter. Replaces cycling through repos one keypress
at a time, which does not scale past a handful of registered repos."""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

_ALL_ID = "repo-filter-all"
_PREFIX = "repo-filter-"


@dataclass(frozen=True)
class RepoFilterPick:
    """Result of a repo-filter selection. ``alias`` is None for "All repos"."""

    alias: str | None


class RepoFilterModal(ModalScreen[RepoFilterPick | None]):
    """Modal listing registered repos to filter by; Enter selects, Esc cancels."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, aliases: list[str], current: str | None = None) -> None:
        """Initialize the picker.

        Args:
            aliases: Registered repo aliases to offer as filters.
            current: The currently-active filter alias, highlighted on open.
        """
        super().__init__()
        self._aliases = aliases
        self._current = current

    def compose(self) -> ComposeResult:
        """Build the title and the repo option list (with an "All repos" entry)."""
        options = [Option("All repos", id=_ALL_ID)]
        options += [Option(alias, id=f"{_PREFIX}{alias}") for alias in self._aliases]
        yield Vertical(
            Static("Filter by repo", classes="modal-title", markup=False),
            OptionList(*options, id="repo-filter-list"),
            classes="modal",
        )

    def on_mount(self) -> None:
        """Focus the list and highlight the active filter, if any."""
        olist = self.query_one(OptionList)
        olist.focus()
        if self._current is not None and self._current in self._aliases:
            olist.highlighted = self._aliases.index(self._current) + 1  # +1 for "All repos"

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Dismiss with the chosen repo (or None alias for "All repos")."""
        option_id = event.option.id or ""
        if option_id == _ALL_ID:
            self.dismiss(RepoFilterPick(alias=None))
        else:
            self.dismiss(RepoFilterPick(alias=option_id[len(_PREFIX) :]))

    def action_cancel(self) -> None:
        """Dismiss the modal without changing the filter."""
        self.dismiss(None)

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        """Cancel on Escape, stopping propagation."""
        if event.key == "escape":
            event.stop()
            self.action_cancel()
