"""Reusable workflow for creating iCloud Hide My Email aliases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ICloudAliasResult:
    owner: Any
    quota: dict[str, Any]
    alias: dict[str, Any]


class ICloudAliasWorkflow:
    """Orchestrate owner resolution, quota lookup, and alias creation."""

    def __init__(self, service: Any):
        self._service = service

    def generate(
        self,
        *,
        owner_email: str = "",
        label: str = "",
        note: str = "",
        proxy: str | None = None,
    ) -> ICloudAliasResult:
        owner = self._service.resolve_account(str(owner_email or ""))
        quota = self._service.alias_quota(owner.id)
        alias = self._service.generate_alias(
            owner.id,
            label=str(label or "").strip(),
            note=str(note or "").strip(),
            proxy=proxy,
        )
        return ICloudAliasResult(owner=owner, quota=dict(quota or {}), alias=dict(alias or {}))
