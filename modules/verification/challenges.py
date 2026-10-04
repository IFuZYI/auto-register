"""Verification challenges and captcha solver adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class VerificationChallenge:
    """A verification request emitted by a platform workflow."""

    kind: str
    page_url: str = ""
    site_key: str = ""
    image_b64: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class VerificationHandler:
    """Adapt a captcha solver to framework verification challenges."""

    def __init__(self, captcha: Any):
        self._captcha = captcha

    def solve(self, challenge: VerificationChallenge) -> str:
        kind = str(challenge.kind or "").strip().lower()
        if kind == "turnstile":
            return self._captcha.solve_turnstile(challenge.page_url, challenge.site_key)
        if kind == "image":
            return self._captcha.solve_image(challenge.image_b64)
        raise ValueError(f"不支持的验证类型: {challenge.kind}")
