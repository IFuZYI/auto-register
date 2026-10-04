"""iCloud 上游错误模型 - 统一错误码，便于 API 层映射 HTTP 状态。"""

from __future__ import annotations

from typing import Optional


class ICloudError(RuntimeError):
    """携带稳定错误码的 iCloud 上游错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retry_after: float = 0.0,
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = max(float(retry_after or 0.0), 0.0)
        self.__cause__ = cause

    def __str__(self) -> str:
        # 有些异常（如 cryptography 的 InvalidTag）str() 为空，直接拼会留下一个"（）"。
        cause = str(self.__cause__) if self.__cause__ is not None else ""
        if not cause.strip():
            return self.message
        return f"{self.message}（{cause}）"


def invalid_config(message: str, cause: Optional[BaseException] = None) -> ICloudError:
    return ICloudError("invalid_config", message, cause=cause)


def invalid_response(message: str, cause: Optional[BaseException] = None) -> ICloudError:
    return ICloudError("invalid_response", message, cause=cause)


def upstream_unavailable(message: str, cause: Optional[BaseException] = None) -> ICloudError:
    return ICloudError("upstream_unavailable", message, cause=cause)


def upstream_rejected(message: str, cause: Optional[BaseException] = None) -> ICloudError:
    return ICloudError("upstream_rejected", message, cause=cause)


def session_expired(message: str) -> ICloudError:
    return ICloudError("session_expired", message)
