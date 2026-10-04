"""Provider-agnostic registration context assembly."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.base_platform import RegisterConfig


@dataclass(frozen=True)
class RegistrationContext:
    """Resolved settings passed from a task to a platform registration workflow."""

    config: RegisterConfig
    settings: dict[str, Any]
    mail_provider: str
    proxy: str | None


class RegistrationContextBuilder:
    """Merge global settings with non-empty task-level overrides."""

    def __init__(self, global_settings: dict[str, Any] | None = None):
        self._global_settings = dict(global_settings or {})

    def build(self, config: RegisterConfig | None = None) -> RegistrationContext:
        resolved_config = config or RegisterConfig()
        settings = dict(self._global_settings)
        settings.update(
            {
                key: value
                for key, value in dict(resolved_config.extra or {}).items()
                if value is not None and value != ""
            }
        )
        return RegistrationContext(
            config=resolved_config,
            settings=settings,
            mail_provider=str(settings.get("mail_provider") or "").strip().lower(),
            proxy=resolved_config.proxy,
        )
