"""Textual TUI for szSwap."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from rich.markup import escape
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.widget import Widget
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Input,
    Label,
    ListItem,
    ListView,
    OptionList,
    Select,
    Static,
)
from textual.widgets.option_list import Option

from szswap.catalog import ClaudeModelSlot, get_claude_model_rows
from szswap.config import (
    AUTH_MODES,
    AppConfig,
    ConfigError,
    ProviderConfig,
    check_env_status,
    default_env_name,
    delete_secret,
    env_value_source,
    load_config,
    load_secrets,
    save_config,
    save_secret,
    validate_base_url,
    validate_env_var_name,
    validate_provider_id,
)
from szswap.engine import (
    EngineError,
    RuntimeStatus,
    apply_hotswap,
    get_status,
    restore_anthropic,
)
from szswap.fetcher import FetchError, FetchResult, fetch_models_async

FetchFn = Callable[..., Awaitable[FetchResult]]

AUTH_MODE_OPTIONS: list[tuple[str, str]] = [
    ("Bearer (ANTHROPIC_AUTH_TOKEN)", "bearer"),
    ("x-api-key (ANTHROPIC_API_KEY)", "x-api-key"),
]

SZSWAP_THEME = Theme(
    name="szswap",
    primary="#60A5FA",
    secondary="#38BDF8",
    accent="#A78BFA",
    success="#4ADE80",
    warning="#FBBF24",
    error="#F87171",
    foreground="#E6EAF2",
    background="#0E1117",
    surface="#151923",
    panel="#222838",
    dark=True,
    variables={
        "border": "#2A3245",
        "border-blurred": "#222838",
        "block-cursor-background": "#263B5E",
        "block-cursor-foreground": "#F8FAFC",
        "block-cursor-blurred-background": "#1D2638",
        "block-cursor-blurred-foreground": "#E6EAF2",
        "block-hover-background": "#1B2334",
        "scrollbar": "#2E374D",
        "scrollbar-hover": "#4B5878",
        "scrollbar-active": "#60A5FA",
        "scrollbar-background": "#11151E",
        "footer-background": "#11151E",
        "footer-key-foreground": "#60A5FA",
        "footer-description-foreground": "#94A3B8",
        "input-cursor-background": "#60A5FA",
        "input-cursor-foreground": "#0E1117",
        "input-selection-background": "#60A5FA 30%",
    },
)

SHARED_WIDGET_CSS = """
Screen {
    background: $background;
    color: $foreground;
}

Input {
    background: #11151E;
    border: round #2A3245;
    color: $foreground;
    height: 3;
    padding: 0 1;
}
Input:hover {
    border: round #3E4A66;
}
Input:focus {
    border: round $primary;
    background: #131926;
}
Input:disabled {
    opacity: 0.55;
}

Select {
    height: auto;
}
Select > SelectCurrent {
    background: #11151E;
    border: round #2A3245;
    padding: 0 1;
}
Select:focus > SelectCurrent {
    border: round $primary;
}

DataTable {
    background: $surface;
    color: $foreground;
    scrollbar-size: 1 1;
}
DataTable > .datatable--header {
    background: #1B2130;
    color: #94A3B8;
    text-style: bold;
}
DataTable > .datatable--odd-row {
    background: #171C28;
}
DataTable > .datatable--even-row {
    background: $surface;
}
DataTable > .datatable--hover {
    background: #20283B;
}
DataTable > .datatable--cursor {
    background: #233350;
    color: #F8FAFC;
    text-style: bold;
}
DataTable:focus > .datatable--cursor {
    background: #2B4570;
    color: #FFFFFF;
    text-style: bold;
}

Button.chip {
    min-width: 8;
    height: 1;
    padding: 0 2;
    border: none;
    background: #1D2332;
    color: #CBD5E1;
    text-style: none;
}
Button.chip:hover {
    background: #2A3348;
    color: #FFFFFF;
}
Button.chip:focus {
    background: #60A5FA;
    color: #0B101B;
    text-style: bold;
}
Button.chip.-primary {
    background: #1B365D;
    color: #93C5FD;
}
Button.chip.-primary:hover {
    background: #24487C;
    color: #FFFFFF;
}
Button.chip.-primary:focus {
    background: #60A5FA;
    color: #0B101B;
    text-style: bold;
}
Button.chip.-success {
    background: #143A2A;
    color: #6EE7B7;
}
Button.chip.-success:hover {
    background: #1B4E39;
    color: #FFFFFF;
}
Button.chip.-success:focus {
    background: #4ADE80;
    color: #071A11;
    text-style: bold;
}
Button.chip.-warning {
    background: #453111;
    color: #FCD34D;
}
Button.chip.-warning:hover {
    background: #5C4116;
    color: #FFFFFF;
}
Button.chip.-warning:focus {
    background: #FBBF24;
    color: #1A1204;
    text-style: bold;
}
Button.chip.-error {
    background: #2C1A20;
    color: #FCA5A5;
}
Button.chip.-error:hover {
    background: #4A222C;
    color: #FFFFFF;
}
Button.chip.-error:focus {
    background: #F87171;
    color: #1D090C;
    text-style: bold;
}
"""


def _focusable_widgets(widgets: list[Widget]) -> list[Widget]:
    return [w for w in widgets if w.display and not w.disabled and w.can_focus]


def _step_focus(widgets: list[Widget], current: Widget | None, delta: int, *, wrap: bool = True) -> bool:
    """Move focus within an ordered list of widgets; returns True if focus moved."""
    items = _focusable_widgets(widgets)
    if not items:
        return False
    if current not in items:
        target = items[0] if delta >= 0 else items[-1]
        target.focus()
        return True
    idx = items.index(current)
    next_idx = idx + delta
    if wrap:
        next_idx %= len(items)
    elif next_idx < 0 or next_idx >= len(items):
        return False
    items[next_idx].focus()
    return True


@dataclass
class LaunchRequest:
    """Returned by SzSwapApp when launching an isolated Mode A Claude session."""

    provider_id: str
    cwd: Path | None = None


class ProviderListItem(ListItem):
    """Two-line provider card bound to a provider ID."""

    DEFAULT_CSS = """
    ProviderListItem {
        height: auto;
        padding: 1 1;
        margin: 0;
        background: transparent;
        color: $foreground;
    }
    """

    def __init__(self, provider: ProviderConfig, *, is_hotswap_active: bool = False) -> None:
        self.provider_id = provider.id
        is_set, _ = check_env_status(provider.auth_env)
        dot = "[#FBBF24]●[/]" if is_hotswap_active else "[#64748B]○[/]"
        key_badge = "[#4ADE80]KEY OK[/]" if is_set else "[#F87171]KEY MISSING[/]"
        ov_count = len(provider.model_overrides)
        cached_count = len(provider.cached_models)
        live_tag = "  [bold #FBBF24]LIVE[/]" if is_hotswap_active else ""

        line1 = f"{dot} [bold #F8FAFC]{escape(provider.id)}[/]  [#CBD5E1]{escape(provider.display_name)}[/]{live_tag}"
        line2 = (
            f"  {key_badge} [#64748B]·[/] "
            f"[#CBD5E1]{ov_count} mapped[/] [#64748B]·[/] "
            f"[#94A3B8]{cached_count} models[/]"
        )
        super().__init__(Static(f"{line1}\n{line2}", markup=True))


class ConfirmHotSwapModal(ModalScreen[str | None]):
    """Confirmation modal before enabling, switching, or disabling live Hot-Swap."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    ConfirmHotSwapModal {
        align: center middle;
        background: rgba(8, 10, 16, 0.76);
    }
    #hotswap-dialog {
        width: 72;
        height: auto;
        border: round $warning;
        border-title-color: $warning;
        border-title-style: bold;
        background: $surface;
        padding: 1 2;
    }
    #hotswap-note {
        color: #94A3B8;
        margin-bottom: 1;
    }
    #hotswap-body {
        margin-bottom: 1;
        padding: 1 2;
        background: #11151E;
    }
    #hotswap-buttons {
        height: auto;
        align: right middle;
        margin-top: 1;
    }
    #hotswap-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, provider: ProviderConfig, active_provider_id: str | None = None) -> None:
        super().__init__()
        self._provider = provider
        self._active_provider_id = active_provider_id

    def compose(self) -> ComposeResult:
        is_switch = bool(self._active_provider_id)
        confirm_label = f"Switch to {self._provider.id}" if is_switch else "Enable Hot-Swap"
        active_line = (
            f"[#64748B]Active   [/] [#FBBF24]{escape(self._active_provider_id)}[/]\n"
            if self._active_provider_id
            else ""
        )
        with Vertical(id="hotswap-dialog"):
            yield Static(
                "Ensure Claude Code is idle before updating ~/.claude/settings.json.\n"
                "The token is written there in plaintext (mode 600). Hot-Swap OFF\n"
                "restores the values that were present before.",
                id="hotswap-note",
                markup=False,
            )
            yield Static(
                (
                    f"{active_line}"
                    f"[#64748B]Target   [/] [bold #F8FAFC]{escape(self._provider.id)}[/] "
                    f"[#94A3B8]({escape(self._provider.display_name)})[/]\n"
                    f"[#64748B]Base URL [/] [#38BDF8]{escape(self._provider.base_url)}[/]\n"
                    f"[#64748B]Overrides[/] [#A78BFA]{len(self._provider.model_overrides)} mapped[/]"
                ),
                id="hotswap-body",
                markup=True,
            )
            with Horizontal(id="hotswap-buttons"):
                yield Button(confirm_label, id="btn-confirm-hotswap", variant="warning", classes="chip", compact=True)
                if is_switch:
                    yield Button(
                        "Turn Hot-Swap Off",
                        id="btn-disable-hotswap",
                        variant="primary",
                        classes="chip",
                        compact=True,
                    )
                yield Button("Cancel", id="btn-cancel-hotswap", classes="chip", compact=True)

    def on_mount(self) -> None:
        dialog = self.query_one("#hotswap-dialog", Vertical)
        dialog.border_title = "Switch Live Hot-Swap" if self._active_provider_id else "Enable Live Hot-Swap"
        dialog.border_subtitle = "←/→ choose · Enter confirm · Esc cancel"
        self.query_one("#btn-confirm-hotswap", Button).focus()

    def _modal_buttons(self) -> list[Widget]:
        return list(self.query("#hotswap-buttons Button"))

    def on_key(self, event: events.Key) -> None:
        if event.key == "left":
            if _step_focus(self._modal_buttons(), self.focused, -1):
                event.stop()
                event.prevent_default()
        elif event.key == "right":
            if _step_focus(self._modal_buttons(), self.focused, 1):
                event.stop()
                event.prevent_default()

    def on_click(self, event: events.Click) -> None:
        if event.widget is self:
            self.dismiss(None)

    @on(Button.Pressed, "#btn-confirm-hotswap")
    def _confirm(self) -> None:
        self.dismiss("apply")

    @on(Button.Pressed, "#btn-disable-hotswap")
    def _disable(self) -> None:
        self.dismiss("off")

    @on(Button.Pressed, "#btn-cancel-hotswap")
    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmDeleteModal(ModalScreen[bool]):
    """Confirmation dialog for deleting a provider."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    ConfirmDeleteModal {
        align: center middle;
        background: rgba(8, 10, 16, 0.76);
    }
    #delete-dialog {
        width: 62;
        height: auto;
        border: round $error;
        border-title-color: $error;
        border-title-style: bold;
        background: $surface;
        padding: 1 2;
    }
    #delete-body {
        margin-bottom: 1;
    }
    #delete-buttons {
        height: auto;
        align: right middle;
        margin-top: 1;
    }
    #delete-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, provider: ProviderConfig) -> None:
        super().__init__()
        self._provider = provider

    def compose(self) -> ComposeResult:
        with Vertical(id="delete-dialog"):
            yield Static(
                (
                    f"Delete provider [bold #F8FAFC]{escape(self._provider.id)}[/] "
                    f"[#94A3B8]({escape(self._provider.display_name)})[/]?\n"
                    "[#64748B]Its stored API key is removed too (unless another provider "
                    "uses the same env var).[/]"
                ),
                id="delete-body",
                markup=True,
            )
            with Horizontal(id="delete-buttons"):
                yield Button("Delete", id="btn-confirm-delete", variant="error", classes="chip", compact=True)
                yield Button("Cancel", id="btn-cancel-delete", classes="chip", compact=True)

    def on_mount(self) -> None:
        dialog = self.query_one("#delete-dialog", Vertical)
        dialog.border_title = "Delete Provider"
        dialog.border_subtitle = "←/→ choose · Esc cancel"
        self.query_one("#btn-cancel-delete", Button).focus()

    def on_key(self, event: events.Key) -> None:
        btns = list(self.query("#delete-buttons Button"))
        if event.key == "left":
            if _step_focus(btns, self.focused, -1):
                event.stop()
                event.prevent_default()
        elif event.key == "right":
            if _step_focus(btns, self.focused, 1):
                event.stop()
                event.prevent_default()

    def on_click(self, event: events.Click) -> None:
        if event.widget is self:
            self.dismiss(False)

    @on(Button.Pressed, "#btn-confirm-delete")
    def _confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#btn-cancel-delete")
    def action_cancel(self) -> None:
        self.dismiss(False)


class ConfirmReplaceProviderModal(ModalScreen[bool]):
    """Confirmation dialog shown before an Add overwrites an existing provider ID."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    ConfirmReplaceProviderModal {
        align: center middle;
        background: rgba(8, 10, 16, 0.76);
    }
    #replace-dialog {
        width: 64;
        height: auto;
        border: round $warning;
        border-title-color: $warning;
        border-title-style: bold;
        background: $surface;
        padding: 1 2;
    }
    #replace-body {
        margin-bottom: 1;
    }
    #replace-buttons {
        height: auto;
        align: right middle;
        margin-top: 1;
    }
    #replace-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, provider: ProviderConfig) -> None:
        super().__init__()
        self._provider = provider

    def compose(self) -> ComposeResult:
        with Vertical(id="replace-dialog"):
            yield Static(
                (
                    f"A provider with id [bold #F8FAFC]{escape(self._provider.id)}[/] already exists.\n"
                    "[#64748B]Saving will overwrite its base URL, auth env, model mappings, "
                    "and cached models.[/]"
                ),
                id="replace-body",
                markup=True,
            )
            with Horizontal(id="replace-buttons"):
                yield Button("Overwrite", id="btn-confirm-replace", variant="warning", classes="chip", compact=True)
                yield Button("Cancel", id="btn-cancel-replace", classes="chip", compact=True)

    def on_mount(self) -> None:
        dialog = self.query_one("#replace-dialog", Vertical)
        dialog.border_title = "Overwrite Provider?"
        dialog.border_subtitle = "←/→ choose · Esc cancel"
        self.query_one("#btn-cancel-replace", Button).focus()

    def on_key(self, event: events.Key) -> None:
        btns = list(self.query("#replace-buttons Button"))
        if event.key == "left":
            if _step_focus(btns, self.focused, -1):
                event.stop()
                event.prevent_default()
        elif event.key == "right":
            if _step_focus(btns, self.focused, 1):
                event.stop()
                event.prevent_default()

    def on_click(self, event: events.Click) -> None:
        if event.widget is self:
            self.dismiss(False)

    @on(Button.Pressed, "#btn-confirm-replace")
    def _confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#btn-cancel-replace")
    def action_cancel(self) -> None:
        self.dismiss(False)


class AddClaudeModelModal(ModalScreen[str | None]):
    """Modal to add a custom Claude model ID row to the mapping table."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    AddClaudeModelModal {
        align: center middle;
        background: rgba(8, 10, 16, 0.76);
    }
    #add-claude-dialog {
        width: 64;
        height: auto;
        border: round $primary;
        border-title-color: $primary;
        border-title-style: bold;
        background: $surface;
        padding: 1 2;
    }
    #inp-custom-claude-model {
        margin-bottom: 1;
    }
    #add-claude-buttons {
        height: auto;
        align: right middle;
    }
    #add-claude-buttons Button {
        margin-left: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="add-claude-dialog"):
            yield Input(placeholder="e.g. claude-opus-6", id="inp-custom-claude-model")
            with Horizontal(id="add-claude-buttons"):
                yield Button("Add Model", id="btn-add-model-ok", variant="primary", classes="chip", compact=True)
                yield Button("Cancel", id="btn-add-model-cancel", classes="chip", compact=True)

    def on_mount(self) -> None:
        dialog = self.query_one("#add-claude-dialog", Vertical)
        dialog.border_title = "Add Custom Claude Model ID"
        dialog.border_subtitle = "Enter to add · Esc cancel"
        self.query_one("#inp-custom-claude-model", Input).focus()

    def on_key(self, event: events.Key) -> None:
        inp = self.query_one("#inp-custom-claude-model", Input)
        btns = list(self.query("#add-claude-buttons Button"))
        if self.focused is inp and event.key == "down":
            btns[0].focus()
            event.stop()
            event.prevent_default()
        elif self.focused in btns:
            if event.key == "up":
                inp.focus()
                event.stop()
                event.prevent_default()
            elif event.key == "left":
                _step_focus(btns, self.focused, -1)
                event.stop()
                event.prevent_default()
            elif event.key == "right":
                _step_focus(btns, self.focused, 1)
                event.stop()
                event.prevent_default()

    def on_click(self, event: events.Click) -> None:
        if event.widget is self:
            self.dismiss(None)

    @on(Input.Submitted, "#inp-custom-claude-model")
    @on(Button.Pressed, "#btn-add-model-ok")
    def _submit(self) -> None:
        value = self.query_one("#inp-custom-claude-model", Input).value.strip()
        if not value:
            self.notify("Enter a Claude model ID.", severity="warning")
            return
        self.dismiss(value)

    @on(Button.Pressed, "#btn-add-model-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class ModelPickerModal(ModalScreen[str | None]):
    """Searchable modal for selecting or typing a provider model replacement."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    ModelPickerModal {
        align: center middle;
        background: rgba(8, 10, 16, 0.76);
    }
    #picker-dialog {
        width: 82;
        height: 26;
        border: round $primary;
        border-title-color: $primary;
        border-title-style: bold;
        background: $surface;
        padding: 1 2;
    }
    #lbl-picker-title {
        color: #94A3B8;
        margin-bottom: 1;
    }
    #inp-model-filter {
        margin-bottom: 1;
    }
    #opt-models {
        height: 1fr;
        margin-bottom: 1;
        background: #11151E;
        border: round #2A3245;
        border-subtitle-color: #64748B;
    }
    #opt-models:focus {
        border: round $primary;
        background-tint: transparent;
    }
    #picker-buttons {
        height: auto;
        align: right middle;
    }
    #picker-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(
        self,
        claude_model_id: str,
        cached_models: list[str],
        current_value: str = "",
    ) -> None:
        super().__init__()
        self._claude_model_id = claude_model_id
        self._cached_models = list(cached_models)
        self._current_value = current_value
        self._filtered_models: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-dialog"):
            yield Static(
                f"Map [bold #F8FAFC]{escape(self._claude_model_id)}[/] → provider model",
                id="lbl-picker-title",
                markup=True,
            )
            yield Input(
                value=self._current_value,
                placeholder="Type to filter or enter custom model ID (↑/↓ navigate list, Enter apply)...",
                id="inp-model-filter",
            )
            yield OptionList(id="opt-models")
            with Horizontal(id="picker-buttons"):
                yield Button("Apply", id="btn-picker-apply", variant="primary", classes="chip", compact=True)
                yield Button("Clear Override", id="btn-picker-clear", variant="warning", classes="chip", compact=True)
                yield Button("Cancel", id="btn-picker-cancel", classes="chip", compact=True)

    def on_mount(self) -> None:
        dialog = self.query_one("#picker-dialog", Vertical)
        dialog.border_title = "Select Provider Model"
        dialog.border_subtitle = "↑/↓ pick · Click or Enter apply · Esc cancel"
        self._refresh_options(self._current_value)
        self.query_one("#inp-model-filter", Input).focus()

    def _refresh_options(self, query_text: str) -> None:
        query = query_text.strip().lower()
        if query:
            self._filtered_models = [m for m in self._cached_models if query in m.lower()]
        else:
            self._filtered_models = list(self._cached_models)

        opt_list = self.query_one("#opt-models", OptionList)
        opt_list.clear_options()
        for model_id in self._filtered_models:
            opt_list.add_option(Option(model_id))
        if self._filtered_models:
            opt_list.highlighted = 0
        opt_list.border_subtitle = f"{len(self._filtered_models)} / {len(self._cached_models)} models"

    def on_key(self, event: events.Key) -> None:
        inp = self.query_one("#inp-model-filter", Input)
        opt_list = self.query_one("#opt-models", OptionList)
        btns = list(self.query("#picker-buttons Button"))

        # Let Up/Down/PageUp/PageDown step through the filtered list directly from the search box.
        if self.focused is inp:
            if event.key == "down":
                if self._filtered_models:
                    cur = opt_list.highlighted
                    if cur is None:
                        opt_list.highlighted = 0
                    elif cur < len(self._filtered_models) - 1:
                        opt_list.action_cursor_down()
                    else:
                        btns[0].focus()
                else:
                    btns[0].focus()
                event.stop()
                event.prevent_default()
            elif event.key == "up":
                if self._filtered_models:
                    opt_list.action_cursor_up()
                event.stop()
                event.prevent_default()
            elif event.key == "pagedown" and self._filtered_models:
                opt_list.action_page_down()
                event.stop()
                event.prevent_default()
            elif event.key == "pageup" and self._filtered_models:
                opt_list.action_page_up()
                event.stop()
                event.prevent_default()
            return

        if self.focused is opt_list:
            if event.key == "up" and (opt_list.highlighted is None or opt_list.highlighted <= 0):
                inp.focus()
                event.stop()
                event.prevent_default()
            elif event.key == "down" and (
                not self._filtered_models
                or opt_list.highlighted is None
                or opt_list.highlighted >= len(self._filtered_models) - 1
            ):
                btns[0].focus()
                event.stop()
                event.prevent_default()
            return

        if self.focused in btns:
            if event.key == "left":
                _step_focus(btns, self.focused, -1)
                event.stop()
                event.prevent_default()
            elif event.key == "right":
                _step_focus(btns, self.focused, 1)
                event.stop()
                event.prevent_default()
            elif event.key == "up":
                if self._filtered_models:
                    opt_list.focus()
                else:
                    inp.focus()
                event.stop()
                event.prevent_default()

    def on_click(self, event: events.Click) -> None:
        if event.widget is self:
            self.dismiss(None)

    @on(Input.Changed, "#inp-model-filter")
    def _on_filter_changed(self, event: Input.Changed) -> None:
        self._refresh_options(event.value)

    @on(OptionList.OptionSelected, "#opt-models")
    def _on_option_selected(self, event: OptionList.OptionSelected) -> None:
        idx = event.option_index
        if 0 <= idx < len(self._filtered_models):
            self.dismiss(self._filtered_models[idx])

    def _resolve_apply_choice(self) -> str | None:
        typed = self.query_one("#inp-model-filter", Input).value.strip()
        opt_list = self.query_one("#opt-models", OptionList)
        highlighted = opt_list.highlighted

        if highlighted is not None and 0 <= highlighted < len(self._filtered_models):
            if not typed or typed in self._filtered_models:
                return typed or self._filtered_models[highlighted]
            return self._filtered_models[highlighted]

        if typed:
            return typed
        return None

    @on(Input.Submitted, "#inp-model-filter")
    @on(Button.Pressed, "#btn-picker-apply")
    def _on_apply(self) -> None:
        choice = self._resolve_apply_choice()
        if choice is None:
            self.notify("Select a model or type a custom model ID.", severity="warning")
            return
        self.dismiss(choice)

    @on(Button.Pressed, "#btn-picker-clear")
    def _on_clear(self) -> None:
        self.dismiss("")

    @on(Button.Pressed, "#btn-picker-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class ProviderEditorScreen(Screen[ProviderConfig | None]):
    """Full-screen form for adding or editing a provider and its model mappings."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    ProviderEditorScreen {
        layout: vertical;
        background: $background;
    }
    #editor-header-bar {
        height: 2;
        padding: 0 2;
        background: #121620;
        border-bottom: solid #222838;
        align: left middle;
    }
    #editor-header {
        width: 1fr;
        text-style: bold;
    }
    #editor-hint {
        width: auto;
        color: #64748B;
    }
    #editor-body {
        height: 1fr;
        padding: 1 2;
    }
    #editor-left {
        width: 48;
        min-width: 38;
        max-width: 56;
        border: round #2A3245;
        border-title-color: #94A3B8;
        border-title-style: bold;
        border-subtitle-color: #64748B;
        background: $surface;
        padding: 0 2 1 2;
    }
    #editor-left:focus-within {
        border: round $primary;
        border-title-color: $primary;
    }
    #editor-form {
        height: 1fr;
        padding-right: 1;
    }
    #editor-mappings {
        width: 1fr;
        margin-left: 1;
        border: round #2A3245;
        border-title-color: #94A3B8;
        border-title-style: bold;
        border-subtitle-color: #64748B;
        background: $surface;
        padding: 0 1 1 1;
    }
    #editor-mappings:focus-within {
        border: round $primary;
        border-title-color: $primary;
    }
    .field-label {
        color: #94A3B8;
        margin-top: 0;
    }
    .field-label-first {
        color: #94A3B8;
        margin-top: 0;
    }
    #lbl-env-status {
        margin-top: 0;
        padding: 0 1;
        height: auto;
    }
    #fetch-row {
        height: auto;
        margin-top: 1;
        padding-top: 1;
        border-top: solid #222838;
        align: left middle;
    }
    #lbl-fetch-status {
        margin-left: 2;
        color: #94A3B8;
        content-align: left middle;
        width: 1fr;
        height: auto;
    }
    #tbl-mappings {
        height: 1fr;
        margin-bottom: 1;
    }
    #mapping-actions {
        height: auto;
        padding: 0 1;
    }
    #mapping-actions Button {
        margin-right: 1;
    }
    #editor-footer {
        height: 2;
        padding: 0 2;
        background: #121620;
        border-top: solid #222838;
        align: left middle;
    }
    #editor-footer Button {
        margin-right: 1;
    }
    #lbl-editor-error {
        margin-left: 2;
        color: $error;
    }
    """

    def __init__(
        self,
        existing: ProviderConfig | None = None,
        claude_default_model: str | None = None,
        fetch_fn: FetchFn = fetch_models_async,
        extra_slots: list[str] | None = None,
    ) -> None:
        super().__init__()
        self._existing = existing
        self._is_edit = existing is not None
        self._claude_default_model = claude_default_model
        self._fetch_fn = fetch_fn
        self._extra_slots = list(extra_slots or [])
        self._cached_models: list[str] = list(existing.cached_models) if existing else []
        self._overrides: dict[str, str] = dict(existing.model_overrides) if existing else {}
        self._custom_claude_models: list[str] = []
        self._row_model_ids: list[str] = []

    def compose(self) -> ComposeResult:
        p = self._existing
        title = f"Edit Provider · {p.id}" if p else "Add Provider"
        auth_mode = p.auth_mode if (p and p.auth_mode in AUTH_MODES) else "bearer"
        models_ep = p.models_endpoint if p else "/v1/models"

        with Horizontal(id="editor-header-bar"):
            yield Static(title, id="editor-header", markup=False)
            yield Static("↑/↓ fields · ←/→ panes & buttons · Esc back", id="editor-hint", markup=False)
        with Horizontal(id="editor-body"):
            with Vertical(id="editor-left"):
                with VerticalScroll(id="editor-form"):
                    yield Label("Provider ID", classes="field-label-first")
                    yield Input(
                        value=p.id if p else "",
                        placeholder="mygw",
                        id="inp-id",
                        disabled=self._is_edit,
                    )
                    yield Label("Display Name", classes="field-label")
                    yield Input(
                        value=p.display_name if p else "",
                        placeholder="My Gateway",
                        id="inp-name",
                    )
                    yield Label("Base URL", classes="field-label")
                    yield Input(
                        value=p.base_url if p else "",
                        placeholder="https://api.example.com",
                        id="inp-url",
                    )
                    yield Label("Auth Mode", classes="field-label")
                    yield Select(
                        options=AUTH_MODE_OPTIONS,
                        value=auth_mode,
                        allow_blank=False,
                        id="sel-auth-mode",
                    )
                    yield Label("Auth Env Var", classes="field-label")
                    yield Input(
                        value=p.auth_env if p else "",
                        placeholder="SZ_API_KEY",
                        id="inp-auth-env",
                    )
                    yield Label("API Key (optional)", classes="field-label")
                    yield Input(
                        value="",
                        placeholder=self._api_key_placeholder(),
                        password=True,
                        id="inp-api-key",
                    )
                    yield Static("", id="lbl-env-status", markup=False)
                    yield Label("Models Endpoint", classes="field-label")
                    yield Input(
                        value=models_ep,
                        placeholder="/v1/models",
                        id="inp-models-ep",
                    )
                    yield Label("Default Claude Model (optional)", classes="field-label")
                    yield Input(
                        value=p.default_model if p else "",
                        placeholder="claude-opus-5-5",
                        id="inp-default-model",
                    )
                with Horizontal(id="fetch-row"):
                    yield Button("Fetch Models", id="btn-fetch-models", classes="chip", compact=True)
                    yield Static(
                        f"Cached: {len(self._cached_models)} models",
                        id="lbl-fetch-status",
                        markup=False,
                    )

            with Vertical(id="editor-mappings"):
                yield DataTable(id="tbl-mappings", zebra_stripes=True)
                with Horizontal(id="mapping-actions"):
                    yield Button("Pick Model", id="btn-pick-model", variant="primary", classes="chip", compact=True)
                    yield Button("Clear", id="btn-clear-mapping", classes="chip", compact=True)
                    yield Button("+ Claude Model", id="btn-add-claude-model", classes="chip", compact=True)

        with Horizontal(id="editor-footer"):
            yield Button("Save Provider", id="btn-save", variant="primary", classes="chip", compact=True)
            yield Button("Cancel", id="btn-cancel", classes="chip", compact=True)
            yield Static("", id="lbl-editor-error", markup=False)

    def _api_key_placeholder(self) -> str:
        if self._existing and self._existing.auth_env:
            stored = load_secrets().get(self._existing.auth_env)
            if stored:
                return "•••••••• stored — type to replace"
        return "paste key (stored 0600 in ~/.szswap/secrets.json)"

    def on_mount(self) -> None:
        left_panel = self.query_one("#editor-left", Vertical)
        left_panel.border_title = "Configuration"
        left_panel.border_subtitle = "key via UI or env var · never in config.toml"

        right_panel = self.query_one("#editor-mappings", Vertical)
        right_panel.border_title = "Model Mapping (Claude → Provider)"
        right_panel.border_subtitle = "Click or Enter row to pick"

        table = self.query_one("#tbl-mappings", DataTable)
        table.cursor_type = "row"
        table.add_columns("Claude Model", "Alias", "Provider Model")
        self._rebuild_mappings_table()
        self._update_env_status(self.query_one("#inp-auth-env", Input).value)

        first_field = self.query_one("#inp-name" if self._is_edit else "#inp-id", Input)
        first_field.focus()

    def _form_fields(self) -> list[Widget]:
        return [
            self.query_one("#inp-id", Input),
            self.query_one("#inp-name", Input),
            self.query_one("#inp-url", Input),
            self.query_one("#sel-auth-mode", Select),
            self.query_one("#inp-auth-env", Input),
            self.query_one("#inp-api-key", Input),
            self.query_one("#inp-models-ep", Input),
            self.query_one("#inp-default-model", Input),
            self.query_one("#btn-fetch-models", Button),
        ]

    def on_key(self, event: events.Key) -> None:
        focused = self.focused
        if focused is None:
            return

        sel = self.query_one("#sel-auth-mode", Select)
        if sel.expanded:
            return

        form_fields = self._form_fields()
        table = self.query_one("#tbl-mappings", DataTable)
        mapping_btns = list(self.query("#mapping-actions Button"))
        footer_btns = list(self.query("#editor-footer Button"))

        # Vertical navigation across form fields.
        if focused in form_fields:
            if event.key == "down":
                if not _step_focus(form_fields, focused, 1, wrap=False):
                    footer_btns[0].focus()
                event.stop()
                event.prevent_default()
                return
            if event.key == "up":
                _step_focus(form_fields, focused, -1, wrap=False)
                event.stop()
                event.prevent_default()
                return
            if event.key == "right" and not isinstance(focused, Input):
                table.focus()
                event.stop()
                event.prevent_default()
                return

        # Right-pane mapping table arrow navigation.
        if focused is table:
            if event.key == "left":
                _step_focus(form_fields, None, 1)
                event.stop()
                event.prevent_default()
                return
            if event.key == "down" and (
                table.row_count == 0 or (table.cursor_row is not None and table.cursor_row >= table.row_count - 1)
            ):
                mapping_btns[0].focus()
                event.stop()
                event.prevent_default()
                return
            return

        # Mapping action buttons row.
        if focused in mapping_btns:
            if event.key == "left":
                if not _step_focus(mapping_btns, focused, -1, wrap=False):
                    self.query_one("#btn-fetch-models", Button).focus()
                event.stop()
                event.prevent_default()
                return
            if event.key == "right":
                _step_focus(mapping_btns, focused, 1, wrap=True)
                event.stop()
                event.prevent_default()
                return
            if event.key == "up":
                table.focus()
                event.stop()
                event.prevent_default()
                return
            if event.key == "down":
                footer_btns[0].focus()
                event.stop()
                event.prevent_default()
                return

        # Footer buttons row.
        if focused in footer_btns:
            if event.key == "left":
                _step_focus(footer_btns, focused, -1, wrap=True)
                event.stop()
                event.prevent_default()
                return
            if event.key == "right":
                _step_focus(footer_btns, focused, 1, wrap=True)
                event.stop()
                event.prevent_default()
                return
            if event.key == "up":
                self.query_one("#btn-fetch-models", Button).focus()
                event.stop()
                event.prevent_default()
                return

    def _update_env_status(self, raw_value: str) -> None:
        lbl = self.query_one("#lbl-env-status", Static)
        text = raw_value.strip()
        if not text:
            lbl.update(Text("▲ Name only, e.g. SZ_API_KEY — or leave blank and paste a key", style="#FBBF24"))
            return
        try:
            env_name = validate_env_var_name(text)
        except ConfigError as exc:
            lbl.update(Text(f"✕ {exc}", style="#F87171"))
            return

        source = env_value_source(env_name)
        is_set, length = check_env_status(env_name)
        if source == "env":
            lbl.update(Text(f"● {env_name} set in your environment (len={length})", style="#4ADE80"))
        elif source == "store":
            lbl.update(Text(f"● {env_name} stored by szswap (len={length})", style="#4ADE80"))
        elif is_set:
            lbl.update(Text(f"● {env_name} set (len={length})", style="#4ADE80"))
        else:
            lbl.update(
                Text(
                    f"▲ {env_name} not set — paste a key above or export it",
                    style="#F87171",
                )
            )

    @on(Input.Changed, "#inp-auth-env")
    def _on_auth_env_changed(self, event: Input.Changed) -> None:
        self._update_env_status(event.value)

    def _get_mapping_slots(self) -> list[ClaudeModelSlot]:
        slots = get_claude_model_rows(
            self._overrides,
            self._claude_default_model,
            self._extra_slots,
        )
        known_ids = {s.id for s in slots}
        for custom_id in self._custom_claude_models:
            if custom_id not in known_ids:
                slots.append(ClaudeModelSlot(id=custom_id, label=custom_id, alias=None))
                known_ids.add(custom_id)
        return slots

    def _rebuild_mappings_table(self, select_model_id: str | None = None) -> None:
        table = self.query_one("#tbl-mappings", DataTable)
        prev_row = table.cursor_row if table.row_count > 0 else 0

        table.clear(columns=False)
        slots = self._get_mapping_slots()
        self._row_model_ids = []
        target_row: int | None = None

        for idx, slot in enumerate(slots):
            replacement = self._overrides.get(slot.id, "")
            alias_cell = Text(slot.alias or "—", style="#64748B")
            target_cell = (
                Text(replacement, style="#A78BFA")
                if replacement
                else Text("— (default)", style="#475569")
            )
            table.add_row(Text(slot.id, style="#E6EAF2"), alias_cell, target_cell, key=slot.id)
            self._row_model_ids.append(slot.id)
            if select_model_id is not None and slot.id == select_model_id:
                target_row = idx

        if self._row_model_ids:
            if target_row is None:
                target_row = min(max(prev_row, 0), len(self._row_model_ids) - 1)
            table.move_cursor(row=target_row)

    def _selected_claude_model_id(self) -> str | None:
        table = self.query_one("#tbl-mappings", DataTable)
        if not self._row_model_ids or table.cursor_row is None:
            return None
        row = table.cursor_row
        if 0 <= row < len(self._row_model_ids):
            return self._row_model_ids[row]
        return None

    def _open_picker_for(self, claude_model_id: str) -> None:
        current = self._overrides.get(claude_model_id, "")

        def _on_picked(result: str | None) -> None:
            if result is None:
                return
            if result == "":
                self._overrides.pop(claude_model_id, None)
            else:
                self._overrides[claude_model_id] = result
            self._rebuild_mappings_table(select_model_id=claude_model_id)

        self.app.push_screen(
            ModelPickerModal(
                claude_model_id=claude_model_id,
                cached_models=self._cached_models,
                current_value=current,
            ),
            _on_picked,
        )

    @on(DataTable.RowSelected, "#tbl-mappings")
    def _on_mapping_row_selected(self, event: DataTable.RowSelected) -> None:
        model_id = str(event.row_key.value) if event.row_key is not None else self._selected_claude_model_id()
        if model_id:
            self._open_picker_for(model_id)

    @on(Button.Pressed, "#btn-pick-model")
    def _on_pick_model_clicked(self) -> None:
        model_id = self._selected_claude_model_id()
        if not model_id:
            self.notify("Select a Claude model row first.", severity="warning")
            return
        self._open_picker_for(model_id)

    @on(Button.Pressed, "#btn-clear-mapping")
    def _on_clear_mapping_clicked(self) -> None:
        model_id = self._selected_claude_model_id()
        if not model_id:
            return
        self._overrides.pop(model_id, None)
        self._rebuild_mappings_table(select_model_id=model_id)

    @on(Button.Pressed, "#btn-add-claude-model")
    def _on_add_claude_model_clicked(self) -> None:
        def _on_added(model_id: str | None) -> None:
            if not model_id:
                return
            if model_id not in self._custom_claude_models:
                self._custom_claude_models.append(model_id)
            self._rebuild_mappings_table(select_model_id=model_id)

        self.app.push_screen(AddClaudeModelModal(), _on_added)

    @on(Button.Pressed, "#btn-fetch-models")
    def _on_fetch_models_clicked(self) -> None:
        self._do_fetch_models()

    @work(exclusive=True)
    async def _do_fetch_models(self) -> None:
        status_lbl = self.query_one("#lbl-fetch-status", Static)
        try:
            base_url = validate_base_url(self.query_one("#inp-url", Input).value)
            auth_env = validate_env_var_name(self.query_one("#inp-auth-env", Input).value)
            auth_mode = str(self.query_one("#sel-auth-mode", Select).value)
            models_ep = self.query_one("#inp-models-ep", Input).value.strip() or "/v1/models"
        except ConfigError as exc:
            status_lbl.update(f"Validation error: {exc}")
            return

        status_lbl.update("Fetching models...")
        try:
            result = await self._fetch_fn(
                base_url=base_url,
                auth_mode=auth_mode,
                auth_env=auth_env,
                models_endpoint=models_ep,
            )
        except (FetchError, ConfigError) as exc:
            status_lbl.update(f"Fetch failed: {exc}")
            return
        except Exception as exc:  # pragma: no cover - unexpected provider payloads
            self.log.exception("Unexpected error while fetching models")
            status_lbl.update(f"Fetch failed ({type(exc).__name__}): {exc}")
            return

        self._cached_models = list(result.models)
        status_lbl.update(f"Fetched {len(result.models)} models from {result.endpoint_used}")

    @on(Button.Pressed, "#btn-save")
    def _on_save_clicked(self) -> None:
        err_lbl = self.query_one("#lbl-editor-error", Static)
        err_lbl.update("")
        try:
            pid = validate_provider_id(self.query_one("#inp-id", Input).value)
            display_name = self.query_one("#inp-name", Input).value.strip() or pid
            base_url = validate_base_url(self.query_one("#inp-url", Input).value)
            auth_mode = str(self.query_one("#sel-auth-mode", Select).value)
            if auth_mode not in AUTH_MODES:
                raise ConfigError(f"Unsupported auth mode: {auth_mode}")
            raw_env = self.query_one("#inp-auth-env", Input).value.strip()
            api_key = self.query_one("#inp-api-key", Input).value.strip()
            if raw_env:
                auth_env = validate_env_var_name(raw_env)
            elif api_key:
                auth_env = default_env_name(pid)
            else:
                raise ConfigError("Enter an env var name or paste an API key.")
            models_ep = self.query_one("#inp-models-ep", Input).value.strip() or "/v1/models"
            default_model = self.query_one("#inp-default-model", Input).value.strip()
        except ConfigError as exc:
            err_lbl.update(str(exc))
            self.notify(str(exc), severity="error")
            return

        if api_key:
            try:
                save_secret(auth_env, api_key)
            except (ConfigError, OSError) as exc:
                message = f"Could not store API key: {exc}"
                err_lbl.update(message)
                self.notify(message, severity="error")
                return
        elif (
            self._existing is not None
            and self._existing.auth_env
            and self._existing.auth_env != auth_env
        ):
            # Renaming the env var should carry the stored key over, not drop it.
            stored = load_secrets().get(self._existing.auth_env)
            if stored:
                try:
                    save_secret(auth_env, stored)
                except (ConfigError, OSError) as exc:
                    message = f"Could not migrate stored API key: {exc}"
                    err_lbl.update(message)
                    self.notify(message, severity="error")
                    return

        clean_overrides = {
            k.strip(): v.strip()
            for k, v in self._overrides.items()
            if k.strip() and v.strip()
        }
        provider = ProviderConfig(
            id=pid,
            display_name=display_name,
            base_url=base_url,
            auth_mode=auth_mode,
            auth_env=auth_env,
            models_endpoint=models_ep,
            default_model=default_model,
            model_overrides=clean_overrides,
            cached_models=list(self._cached_models),
        )
        self.dismiss(provider)

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class SzSwapApp(App[LaunchRequest | None]):
    """Main Textual TUI for managing Claude Code providers and model overrides."""

    TITLE = "szSwap"
    SUB_TITLE = "Claude Code Provider & Model Switcher"

    BINDINGS = [
        Binding("a", "add_provider", "Add"),
        Binding("e", "edit_provider", "Edit"),
        Binding("f", "fetch_models", "Fetch"),
        Binding("r", "run_isolated", "Launch"),
        Binding("u", "toggle_hotswap", "Hot-Swap"),
        Binding("x", "delete_provider", "Delete"),
        Binding("q", "quit", "Quit"),
    ]

    CSS = SHARED_WIDGET_CSS + """
    #top-bar {
        height: 2;
        padding: 0 2;
        background: #121620;
        border-bottom: solid #222838;
        align: left middle;
    }
    #brand-badge {
        width: auto;
        margin-right: 2;
    }
    #lbl-mode-status {
        width: 1fr;
    }
    #lbl-claude-account {
        width: auto;
        text-align: right;
    }
    #main-panes {
        height: 1fr;
        padding: 1 2 0 2;
    }
    #left-pane {
        width: 42;
        min-width: 34;
        max-width: 48;
        border: round #2A3245;
        border-title-color: #94A3B8;
        border-title-style: bold;
        border-subtitle-color: #64748B;
        background: $surface;
        padding: 0 1;
    }
    #left-pane:focus-within {
        border: round $primary;
        border-title-color: $primary;
    }
    #provider-list {
        height: 1fr;
        background: transparent;
        border: none;
        padding: 0;
    }
    #provider-list:focus {
        background-tint: transparent;
    }
    #right-pane {
        width: 1fr;
        margin-left: 1;
    }
    #details-panel {
        height: auto;
        border: round #2A3245;
        border-title-color: #94A3B8;
        border-title-style: bold;
        border-subtitle-color: #64748B;
        background: $surface;
        padding: 0 2;
    }
    #lbl-provider-details {
        height: auto;
    }
    #overrides-panel {
        height: 1fr;
        margin-top: 1;
        border: round #2A3245;
        border-title-color: #94A3B8;
        border-title-style: bold;
        border-subtitle-color: #64748B;
        background: $surface;
        padding: 0 1;
    }
    #overrides-panel:focus-within {
        border: round $primary;
        border-title-color: $primary;
    }
    #tbl-detail-overrides {
        height: 1fr;
    }
    #lbl-overrides-empty {
        height: 1fr;
        content-align: center middle;
        color: #64748B;
        display: none;
    }
    #action-bar {
        height: 3;
        padding: 1 2;
        align: left middle;
    }
    #action-bar Button {
        margin-right: 1;
    }
    #action-spacer {
        width: 1fr;
    }
    #btn-delete {
        margin-right: 0;
    }
    Footer {
        background: #11151E;
    }
    """

    def __init__(
        self,
        *,
        fetch_fn: FetchFn | None = None,
        config_path: Path | None = None,
        claude_dir: Path | None = None,
        launch_cwd: Path | None = None,
    ) -> None:
        super().__init__()
        self.register_theme(SZSWAP_THEME)
        self.theme = "szswap"
        self._fetch_fn: FetchFn = fetch_fn or fetch_models_async
        self._config_path = config_path
        self._claude_dir = claude_dir
        self._launch_cwd = launch_cwd
        self.cfg: AppConfig = load_config(path=config_path)
        self.selected_provider_id: str | None = None
        self._last_status: RuntimeStatus | None = None
        self._last_pane: str = "left"
        self._detail_override_keys: list[str] = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="top-bar"):
            yield Static("[bold #60A5FA]szSwap[/] [#334155]│[/]", id="brand-badge", markup=True)
            yield Static("", id="lbl-mode-status", markup=True)
            yield Static("", id="lbl-claude-account", markup=True)
        with Horizontal(id="main-panes"):
            with Vertical(id="left-pane"):
                yield ListView(id="provider-list")
            with Vertical(id="right-pane"):
                with Vertical(id="details-panel"):
                    yield Static("", id="lbl-provider-details", markup=True)
                with Vertical(id="overrides-panel"):
                    yield DataTable(id="tbl-detail-overrides", zebra_stripes=True)
                    yield Static(
                        "No model overrides configured — press 'e' or click Edit / Map to add.",
                        id="lbl-overrides-empty",
                        markup=False,
                    )
        with Horizontal(id="action-bar"):
            yield Button("+ Add Provider", id="btn-add", variant="primary", classes="chip", compact=True)
            yield Button("Edit / Map", id="btn-edit", classes="chip", compact=True)
            yield Button("Fetch", id="btn-fetch", classes="chip", compact=True)
            yield Button("▶ Launch", id="btn-run", variant="success", classes="chip", compact=True)
            yield Button("Hot-Swap: OFF", id="btn-hotswap", variant="default", classes="chip", compact=True)
            yield Static("", id="action-spacer")
            yield Button("Delete", id="btn-delete", variant="error", classes="chip", compact=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#left-pane", Vertical).border_title = "Providers"
        self.query_one("#details-panel", Vertical).border_title = "Provider Details"
        overrides_panel = self.query_one("#overrides-panel", Vertical)
        overrides_panel.border_title = "Model Overrides"
        overrides_panel.border_subtitle = "Click or Enter row to quick-map"

        detail_tbl = self.query_one("#tbl-detail-overrides", DataTable)
        detail_tbl.cursor_type = "row"
        detail_tbl.add_columns("Claude Model", "Provider Model")
        self._refresh_all()
        self.query_one("#provider-list", ListView).focus()

    def _action_buttons(self) -> list[Widget]:
        return list(self.query("#action-bar Button"))

    def on_key(self, event: events.Key) -> None:
        # Only intercept arrow navigation on the main dashboard screen.
        if len(self.screen_stack) > 1:
            return

        focused = self.focused
        lv = self.query_one("#provider-list", ListView)
        tbl = self.query_one("#tbl-detail-overrides", DataTable)
        btns = self._action_buttons()

        if focused is lv:
            self._last_pane = "left"
            if event.key == "right":
                if tbl.display and tbl.row_count > 0:
                    tbl.focus()
                else:
                    btns[0].focus()
                event.stop()
                event.prevent_default()
                return
            if event.key == "down":
                count = len(lv.children)
                if count == 0 or (lv.index is not None and lv.index >= count - 1):
                    btns[0].focus()
                    event.stop()
                    event.prevent_default()
                    return
            if event.key == "enter" and self._get_selected_provider() is not None:
                self.action_edit_provider()
                event.stop()
                event.prevent_default()
                return
            return

        if focused is tbl:
            self._last_pane = "right"
            if event.key == "left":
                lv.focus()
                event.stop()
                event.prevent_default()
                return
            if event.key == "right":
                self.query_one("#btn-edit", Button).focus()
                event.stop()
                event.prevent_default()
                return
            if event.key == "down":
                if tbl.row_count == 0 or (tbl.cursor_row is not None and tbl.cursor_row >= tbl.row_count - 1):
                    self.query_one("#btn-edit", Button).focus()
                    event.stop()
                    event.prevent_default()
                    return
            return

        if focused in btns:
            if event.key == "left":
                _step_focus(btns, focused, -1, wrap=True)
                event.stop()
                event.prevent_default()
                return
            if event.key == "right":
                _step_focus(btns, focused, 1, wrap=True)
                event.stop()
                event.prevent_default()
                return
            if event.key == "up":
                if self._last_pane == "right" and tbl.display and tbl.row_count > 0:
                    tbl.focus()
                else:
                    lv.focus()
                event.stop()
                event.prevent_default()
                return

    def on_click(self, event: events.Click) -> None:
        if len(self.screen_stack) > 1:
            return
        # Double-clicking a provider item opens the editor directly.
        if event.chain == 2 and event.widget is not None:
            widget: Widget | None = event.widget
            while widget is not None:
                if isinstance(widget, ProviderListItem):
                    self.selected_provider_id = widget.provider_id
                    self.action_edit_provider()
                    return
                widget = widget.parent if isinstance(widget.parent, Widget) else None

    def _get_selected_provider(self) -> ProviderConfig | None:
        if self.selected_provider_id and self.selected_provider_id in self.cfg.providers:
            return self.cfg.providers[self.selected_provider_id]
        return None

    def _refresh_status_banner(self) -> None:
        status = get_status(self.cfg, claude_dir=self._claude_dir)
        self._last_status = status

        tel_text = "nonessential=OFF" if status.nonessential_disabled else "nonessential=default"
        tel_color = "#4ADE80" if status.nonessential_disabled else "#64748B"

        if status.mode == "anthropic":
            mode_markup = (
                "[#4ADE80]●[/] [bold #E6EAF2]Hot-Swap: OFF[/] "
                "[#94A3B8](Anthropic Direct)[/]"
            )
        else:
            pid_label = escape(status.matched_provider_id or "custom")
            url_label = escape(status.base_url or "")
            mode_markup = (
                f"[#FBBF24]●[/] [bold #FBBF24]Hot-Swap: ON[/] "
                f"[#E6EAF2]— {pid_label}[/] [#38BDF8]({url_label})[/]"
            )

        account_email = escape(status.account_email or "not detected")
        self.query_one("#lbl-mode-status", Static).update(mode_markup)
        self.query_one("#lbl-claude-account", Static).update(
            f"[#64748B]Account[/] [#CBD5E1]{account_email}[/]  "
            f"[#334155]│[/]  [{tel_color}]{tel_text}[/]"
        )
        self._refresh_hotswap_button()

    def _refresh_hotswap_button(self) -> None:
        btn = self.query_one("#btn-hotswap", Button)
        status = self._last_status
        if status is None or status.mode == "anthropic":
            btn.label = "Hot-Swap: OFF"
            btn.variant = "default"
            return
        active_id = status.matched_provider_id or "custom"
        btn.label = f"Hot-Swap: ON ({active_id})"
        btn.variant = "warning"

    def _refresh_provider_list(self) -> None:
        lv = self.query_one("#provider-list", ListView)
        lv.clear()
        provider_ids = sorted(self.cfg.providers.keys())
        active_id = (
            self._last_status.matched_provider_id
            if (self._last_status and self._last_status.mode != "anthropic")
            else None
        )

        if self.selected_provider_id not in self.cfg.providers:
            if self.cfg.default_provider in self.cfg.providers:
                self.selected_provider_id = self.cfg.default_provider
            elif provider_ids:
                self.selected_provider_id = provider_ids[0]
            else:
                self.selected_provider_id = None

        selected_idx: int | None = None
        for idx, pid in enumerate(provider_ids):
            lv.append(
                ProviderListItem(
                    self.cfg.providers[pid],
                    is_hotswap_active=(pid == active_id),
                )
            )
            if pid == self.selected_provider_id:
                selected_idx = idx

        count = len(provider_ids)
        self.query_one("#left-pane", Vertical).border_subtitle = (
            f"{count} configured" if count else "empty"
        )

        if selected_idx is not None:
            lv.index = selected_idx

    def _refresh_provider_details(self, select_model_id: str | None = None) -> None:
        details_panel = self.query_one("#details-panel", Vertical)
        details_lbl = self.query_one("#lbl-provider-details", Static)
        detail_tbl = self.query_one("#tbl-detail-overrides", DataTable)
        empty_lbl = self.query_one("#lbl-overrides-empty", Static)
        overrides_panel = self.query_one("#overrides-panel", Vertical)

        prev_row = detail_tbl.cursor_row if detail_tbl.row_count > 0 else 0
        detail_tbl.clear(columns=False)
        self._detail_override_keys = []

        provider = self._get_selected_provider()
        if provider is None:
            details_panel.border_subtitle = ""
            details_lbl.update(
                "[#94A3B8]No provider selected.[/]\n"
                "[#64748B]Press 'a' or click + Add Provider to configure a gateway.[/]"
            )
            detail_tbl.display = False
            empty_lbl.display = True
            overrides_panel.border_subtitle = ""
            return

        is_set, length = check_env_status(provider.auth_env)
        source = env_value_source(provider.auth_env) if provider.auth_env else None
        if source == "env":
            env_badge = f"[#4ADE80]● ENV SET (len={length})[/]"
        elif source == "store":
            env_badge = f"[#4ADE80]● STORED (len={length})[/]"
        elif is_set:
            env_badge = f"[#4ADE80]● SET (len={length})[/]"
        else:
            env_badge = "[#F87171]▲ NOT SET[/]"
        details_panel.border_subtitle = "KEY OK" if is_set else "KEY MISSING"

        default_model = (
            f"[#A78BFA]{escape(provider.default_model)}[/]"
            if provider.default_model
            else "[#64748B](inherit Claude default)[/]"
        )

        details_lbl.update(
            "\n".join(
                [
                    f"[bold #F8FAFC]{escape(provider.display_name)}[/]  "
                    f"[#64748B]({escape(provider.id)})[/]   "
                    f"[#38BDF8]{escape(provider.base_url)}[/]",
                    f"[#64748B]Auth     [/] [#CBD5E1]{escape(provider.auth_mode)}[/]  "
                    f"[#334155]·[/]  [#E6EAF2]{escape(provider.auth_env)}[/]  {env_badge}",
                    f"[#64748B]Models   [/] [#CBD5E1]{escape(provider.models_endpoint)}[/]  "
                    f"[#334155]·[/]  [#94A3B8]{len(provider.cached_models)} cached[/]",
                    f"[#64748B]Default  [/] {default_model}",
                ]
            )
        )

        claude_default = (
            self._last_status.claude_default_model if self._last_status is not None else None
        )
        slots = get_claude_model_rows(
            provider.model_overrides,
            claude_default,
            self.cfg.claude_model_slots,
        )
        mapped_count = sum(1 for s in slots if provider.model_overrides.get(s.id))
        detail_tbl.display = True
        empty_lbl.display = False
        overrides_panel.border_subtitle = (
            f"{mapped_count}/{len(slots)} mapped · Click / Enter to quick-map"
        )
        target_row: int | None = None
        for idx, slot in enumerate(slots):
            target_model = provider.model_overrides.get(slot.id, "")
            target_cell = (
                Text(target_model, style="#A78BFA")
                if target_model
                else Text("— (default)", style="#475569")
            )
            detail_tbl.add_row(
                Text(slot.id, style="#E6EAF2"),
                target_cell,
                key=slot.id,
            )
            self._detail_override_keys.append(slot.id)
            if select_model_id is not None and slot.id == select_model_id:
                target_row = idx

        if self._detail_override_keys:
            if target_row is None:
                target_row = min(max(prev_row, 0), len(self._detail_override_keys) - 1)
            detail_tbl.move_cursor(row=target_row)

    def _refresh_all(self) -> None:
        self._refresh_status_banner()
        self._refresh_provider_list()
        self._refresh_provider_details()

    @on(ListView.Highlighted, "#provider-list")
    @on(ListView.Selected, "#provider-list")
    def _on_provider_list_change(self, event: ListView.Highlighted | ListView.Selected) -> None:
        item = event.item
        if isinstance(item, ProviderListItem):
            self.selected_provider_id = item.provider_id
            self._refresh_provider_details()
            self._refresh_hotswap_button()

    @on(DataTable.RowSelected, "#tbl-detail-overrides")
    def _on_detail_override_selected(self, event: DataTable.RowSelected) -> None:
        """Quick-map a Claude model override directly from the dashboard."""
        provider = self._get_selected_provider()
        if provider is None:
            return
        claude_model = (
            str(event.row_key.value)
            if event.row_key is not None
            else (
                self._detail_override_keys[event.cursor_row]
                if 0 <= event.cursor_row < len(self._detail_override_keys)
                else None
            )
        )
        if not claude_model:
            return
        current = provider.model_overrides.get(claude_model, "")

        def _on_quick_picked(result: str | None) -> None:
            if result is None:
                return
            if result == "":
                provider.model_overrides.pop(claude_model, None)
            else:
                provider.model_overrides[claude_model] = result
            if not self._safe_save_config():
                self._refresh_all()
                return
            self._refresh_provider_list()
            self._refresh_provider_details(select_model_id=claude_model)
            self.notify(
                f"Updated {claude_model} → {result or '(cleared)'}."
            )

        self.push_screen(
            ModelPickerModal(
                claude_model_id=claude_model,
                cached_models=provider.cached_models,
                current_value=current,
            ),
            _on_quick_picked,
        )

    def _safe_save_config(self) -> bool:
        """Persist config, reporting failures instead of crashing the TUI."""
        try:
            save_config(self.cfg, path=self._config_path)
            return True
        except (ConfigError, OSError) as exc:
            self.notify(f"Failed to save config: {exc}", severity="error")
            self.cfg = load_config(path=self._config_path)
            return False

    def _persist_provider(self, provider: ProviderConfig) -> None:
        previous = self.cfg.providers.get(provider.id)
        self.cfg.providers[provider.id] = provider
        if not self.cfg.default_provider:
            self.cfg.default_provider = provider.id
        if not self._safe_save_config():
            self._refresh_all()
            return
        if (
            previous is not None
            and previous.auth_env
            and previous.auth_env != provider.auth_env
            and not any(
                p.auth_env == previous.auth_env for p in self.cfg.providers.values()
            )
        ):
            delete_secret(previous.auth_env)
        self.selected_provider_id = provider.id
        self._refresh_all()
        self.notify(f"Saved provider '{provider.id}'.")

    def _on_provider_saved(self, provider: ProviderConfig | None) -> None:
        if provider is None:
            return
        self._persist_provider(provider)

    def _on_provider_added(self, provider: ProviderConfig | None) -> None:
        if provider is None:
            return
        if provider.id not in self.cfg.providers:
            self._persist_provider(provider)
            return

        def _on_replace_confirmed(confirmed: bool) -> None:
            if confirmed:
                self._persist_provider(provider)

        self.push_screen(ConfirmReplaceProviderModal(provider), _on_replace_confirmed)

    @on(Button.Pressed, "#btn-add")
    def action_add_provider(self) -> None:
        claude_default = self._last_status.claude_default_model if self._last_status else None
        self.push_screen(
            ProviderEditorScreen(
                existing=None,
                claude_default_model=claude_default,
                fetch_fn=self._fetch_fn,
                extra_slots=self.cfg.claude_model_slots,
            ),
            self._on_provider_added,
        )

    @on(Button.Pressed, "#btn-edit")
    def action_edit_provider(self) -> None:
        provider = self._get_selected_provider()
        if provider is None:
            self.notify("Select a provider to edit.", severity="warning")
            return
        claude_default = self._last_status.claude_default_model if self._last_status else None
        self.push_screen(
            ProviderEditorScreen(
                existing=provider,
                claude_default_model=claude_default,
                fetch_fn=self._fetch_fn,
                extra_slots=self.cfg.claude_model_slots,
            ),
            self._on_provider_saved,
        )

    @on(Button.Pressed, "#btn-fetch")
    def action_fetch_models(self) -> None:
        provider = self._get_selected_provider()
        if provider is None:
            self.notify("Select a provider to fetch models.", severity="warning")
            return
        self._fetch_selected_provider(provider)

    @work(exclusive=True)
    async def _fetch_selected_provider(self, provider: ProviderConfig) -> None:
        try:
            result = await self._fetch_fn(
                base_url=provider.base_url,
                auth_mode=provider.auth_mode,
                auth_env=provider.auth_env,
                models_endpoint=provider.models_endpoint,
            )
        except (FetchError, ConfigError) as exc:
            self.notify(f"Fetch failed for '{provider.id}': {exc}", severity="error")
            return
        except Exception as exc:  # pragma: no cover - unexpected provider payloads
            self.log.exception("Unexpected error while fetching models")
            self.notify(
                f"Fetch failed for '{provider.id}' ({type(exc).__name__}): {exc}",
                severity="error",
            )
            return

        provider.cached_models = list(result.models)
        if not self._safe_save_config():
            return
        self._refresh_provider_list()
        self._refresh_provider_details()
        self.notify(f"Fetched {len(result.models)} models from {result.endpoint_used}.")

    @on(Button.Pressed, "#btn-run")
    def action_run_isolated(self) -> None:
        provider = self._get_selected_provider()
        if provider is None:
            self.notify("Select a provider to run.", severity="warning")
            return
        is_set, _ = check_env_status(provider.auth_env)
        if not is_set:
            self.notify(
                f"Cannot launch '{provider.id}': env var '{provider.auth_env}' is not set.",
                severity="error",
            )
            return
        self.exit(LaunchRequest(provider_id=provider.id, cwd=self._launch_cwd))

    @on(Button.Pressed, "#btn-hotswap")
    def action_toggle_hotswap(self) -> None:
        status = self._last_status or get_status(self.cfg, claude_dir=self._claude_dir)
        provider = self._get_selected_provider()

        # If Hot-Swap is already ON for the selected provider (or no different provider selected), toggle OFF.
        if status.mode != "anthropic":
            active_id = status.matched_provider_id or "custom"
            if provider is None or provider.id == active_id:
                self._disable_hotswap()
                return
            is_set, _ = check_env_status(provider.auth_env)
            if not is_set:
                self.notify(
                    f"Cannot switch Hot-Swap to '{provider.id}': env var '{provider.auth_env}' is not set.",
                    severity="error",
                )
                return

            def _on_switch_choice(choice: str | None) -> None:
                if choice == "off":
                    self._disable_hotswap()
                elif choice == "apply":
                    self._enable_hotswap(provider)

            self.push_screen(
                ConfirmHotSwapModal(provider, active_provider_id=active_id),
                _on_switch_choice,
            )
            return

        # Hot-Swap is currently OFF -> enable for selected provider.
        if provider is None:
            self.notify("Select a provider to enable Hot-Swap.", severity="warning")
            return
        is_set, _ = check_env_status(provider.auth_env)
        if not is_set:
            self.notify(
                f"Cannot enable Hot-Swap for '{provider.id}': env var '{provider.auth_env}' is not set.",
                severity="error",
            )
            return

        def _on_enable_choice(choice: str | None) -> None:
            if choice == "apply":
                self._enable_hotswap(provider)

        self.push_screen(ConfirmHotSwapModal(provider), _on_enable_choice)

    def _enable_hotswap(self, provider: ProviderConfig) -> None:
        try:
            apply_hotswap(provider, claude_dir=self._claude_dir)
        except (EngineError, ConfigError, OSError) as exc:
            self.notify(f"Hot-Swap failed: {exc}", severity="error")
            return
        self._refresh_all()
        self.notify(f"Hot-Swap ON: '{provider.id}'.")

    def _disable_hotswap(self) -> None:
        try:
            restore_anthropic(claude_dir=self._claude_dir)
        except (EngineError, OSError) as exc:
            self.notify(f"Failed to turn Hot-Swap off: {exc}", severity="error")
            return
        self._refresh_all()
        self.notify("Hot-Swap OFF (Anthropic Direct).")

    @on(Button.Pressed, "#btn-delete")
    def action_delete_provider(self) -> None:
        provider = self._get_selected_provider()
        if provider is None:
            self.notify("Select a provider to delete.", severity="warning")
            return

        def _on_confirmed(confirmed: bool) -> None:
            if not confirmed:
                return
            self.cfg.providers.pop(provider.id, None)
            if self.cfg.default_provider == provider.id:
                self.cfg.default_provider = ""
            if not self._safe_save_config():
                self._refresh_all()
                return
            if provider.auth_env and not any(
                p.auth_env == provider.auth_env for p in self.cfg.providers.values()
            ):
                delete_secret(provider.auth_env)
            self.selected_provider_id = None
            self._refresh_all()
            self.notify(f"Deleted provider '{provider.id}'.")

        self.push_screen(ConfirmDeleteModal(provider), _on_confirmed)
