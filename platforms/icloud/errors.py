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


#: 「再试一次可能就好了」的错误码：上游暂时不可达 / 服务端内部故障 / 会话过期。
#:
#: 这些错误在读验证码时**不该判整轮失败** —— 多等一个轮询周期往往就恢复了。
#: 反过来 `invalid_config` / `permission_denied` / `account_disabled`
#: 重试多少次都是同一结局，必须立刻抛。
RECOVERABLE_CODES = frozenset(
    {"upstream_unavailable", "upstream_error", "session_expired"}
)


def is_recoverable(exc: BaseException) -> bool:
    """这个异常是否值得「再读一次」而不是当场判失败。"""
    return isinstance(exc, ICloudError) and exc.code in RECOVERABLE_CODES


#: 「服务本身现在不可用」的错误码：重开一轮注册只会再等一次超时，
#: 换号也没用 —— 该停下来告诉用户，而不是继续烧别名和重试轮次。
OUTAGE_CODES = frozenset({"upstream_unavailable", "upstream_error"})


def is_upstream_outage(exc: BaseException) -> bool:
    """这次失败是否应归因于**上游服务**，而不是这个账号/这封邮件。

    与 `is_recoverable` 的分工：后者回答「这一轮里要不要再读一次」，
    这里回答「整批任务还要不要继续试」。实测（2026-11-13）两批任务里
    约 80% 的失败是 `icloud-hme` 读超时 —— 把它们判成可重试，等于拿
    重试轮次去换一个必然相同的结局。

    刻意**不**包含：
    - `otp_timeout`（没收到码 = 这个号/这封信的问题，换号有意义）；
    - `provider_rate_limited`（主号额度用完，等一下还能建别名）；
    - `session_expired`（凭据问题，要用户重新导入，不是服务挂了）。
    """
    if isinstance(exc, ICloudError):
        return exc.code in OUTAGE_CODES
    # 熔断打开时抛的 UpstreamBusy 就是「服务不可用」的直接表达。
    from services.icloud.upstream_gate import is_upstream_busy

    return is_upstream_busy(exc)
