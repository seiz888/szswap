"""Catalog of Claude model slots for override mapping."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ClaudeModelSlot:
    """Represents a selectable Claude model slot in the override matrix."""

    id: str
    label: str
    alias: str | None = None


DEFAULT_CLAUDE_MODELS: tuple[ClaudeModelSlot, ...] = (
    ClaudeModelSlot(id="claude-opus-5-5", label="Opus 5.5 (latest)", alias="opus"),
    ClaudeModelSlot(id="claude-sonnet-5-5", label="Sonnet 5.5 (latest)", alias="sonnet"),
    ClaudeModelSlot(id="claude-fable-5-2", label="Fable 5.2 (latest)", alias="fable"),
    ClaudeModelSlot(id="claude-haiku-5-5", label="Haiku 5.5 (latest)", alias="haiku"),
    ClaudeModelSlot(id="claude-fable-5-1", label="Fable 5.1"),
    ClaudeModelSlot(id="claude-haiku-4-5-20251001", label="Haiku 4.5"),
    ClaudeModelSlot(id="claude-sonnet-5", label="Sonnet 5"),
    ClaudeModelSlot(id="claude-opus-5", label="Opus 5"),
    ClaudeModelSlot(id="claude-fable-5", label="Fable 5"),
    ClaudeModelSlot(id="claude-opus-4-8", label="Opus 4.8"),
    ClaudeModelSlot(id="claude-opus-4-7", label="Opus 4.7"),
    ClaudeModelSlot(id="claude-opus-4-6", label="Opus 4.6"),
    ClaudeModelSlot(id="claude-sonnet-4-6", label="Sonnet 4.6"),
)


def get_claude_model_rows(
    existing_overrides: dict[str, str],
    claude_settings_model: str | None = None,
) -> list[ClaudeModelSlot]:
    """Return ordered Claude model slots including active and custom overrides."""
    rows: list[ClaudeModelSlot] = list(DEFAULT_CLAUDE_MODELS)
    seen_ids: set[str] = {slot.id for slot in rows}
    known_aliases: set[str] = {slot.alias for slot in rows if slot.alias}

    if claude_settings_model:
        active = claude_settings_model.strip()
        if active and active not in seen_ids and active not in known_aliases:
            rows.append(ClaudeModelSlot(id=active, label=f"{active} (active)"))
            seen_ids.add(active)

    for raw_key in existing_overrides:
        key = raw_key.strip()
        if key and key not in seen_ids:
            rows.append(ClaudeModelSlot(id=key, label=f"{key} (custom)"))
            seen_ids.add(key)

    return rows
