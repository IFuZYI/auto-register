"""注册线路验证的公共夹具（Task 1 产物，Task 2-7 复用）。

为什么要有它
------------
「测试全绿」不等于「线路能跑」——2978 条全绿时注册仍在失败。把「一条线路
真的跑通」变成可重复的断言，需要三件东西（见
`.superpowers/sdd/full-route-verification-plan/task-1-brief.md`）：

1. `FakeHttp`：按顺序喂预设响应、记录每次请求 —— 用**真实调用链**驱动线路，
   只替掉网络出口（不 mock 实现本身）；
2. `assert_account_persisted` / `assert_no_secret_leak`：断言**最终产物**
   （账号落库、凭证完整、状态正常）与「日志/响应里没有明文凭证」；
3. `make_account` / `cleanup_accounts`：测试数据的建与清。

约束（项目既有，违反会变红）
----------------------------
- 读账号一律走 ``core.db.account_repository``（平台分库与 JSON 后端下才正确），
  不直接查 SQLite；
- 不 mock 后端判定（``resolve_backend_name`` / ``accounts_backend_enabled`` /
  ``sqlite_db_exists``）——测试都在真实后端下跑；
- 测试自己造的数据自己删（``cleanup_accounts``）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from core.base_platform import Account, AccountStatus
from core.credential_fields import get_credential
from core.db import account_repository
from core.registry import SUPPORTED_PLATFORMS


class FakeResponse:
    """一次 HTTP 响应的替身（只带线路代码真正用到的三个成员）。"""

    def __init__(
        self, status_code: int = 200, text: str = "", json_body: dict | None = None
    ) -> None:
        self.status_code = status_code
        #: json_body 给定时 text 自动序列化，两个视图保持一致
        self.text = text if json_body is None else json.dumps(json_body, ensure_ascii=False)
        self._json_body = json_body

    def json(self) -> dict:
        """返回 JSON 体：给了 ``json_body`` 就用它，否则解析 ``text``（空体 → {}）。"""
        if self._json_body is not None:
            return self._json_body
        return json.loads(self.text) if self.text else {}


@dataclass
class RecordedRequest:
    """``FakeHttp`` 记下的一次请求（供断言 URL / body / headers）。"""

    url: str
    method: str
    body: dict | str = ""
    headers: dict = field(default_factory=dict)


class FakeHttp:
    """按顺序返回预设响应的 session 替身。

    用法::

        http = FakeHttp([a_response({"access_token": "at-1"}), OSError("boom")])
        with http as session:
            ...
        assert session.requests[0].url == "..."

    预设项可以是 `FakeResponse`、裸 dict（当作 JSON 体）、或异常实例（轮到它
    时抛出，用来模拟连接失败）。

    **用尽即抛** `AssertionError`：响应不够时静默复用上一条，会让「线路多发了
    请求」这种缺陷隐身 —— 尤其是注册线路，多发的请求往往就是要修的 bug。
    """

    def __init__(self, responses: list[FakeResponse | dict | Exception]) -> None:
        self._responses = list(responses)
        self._cursor = 0
        #: 记录所有发出的请求（供断言 URL / body / headers）
        self.requests: list[RecordedRequest] = []

    def _respond(self, method: str, url: str, kwargs: dict) -> FakeResponse:
        if kwargs.get("json") is not None:
            body: dict | str = kwargs["json"]
        else:
            body = kwargs.get("data") or ""
        self.requests.append(
            RecordedRequest(
                url=url,
                method=method,
                body=body,
                headers=dict(kwargs.get("headers") or {}),
            )
        )
        if self._cursor >= len(self._responses):
            raise AssertionError(
                f"FakeHttp 没有为 {url} 准备响应（已发出 {len(self.requests)} 个请求）"
            )
        item = self._responses[self._cursor]
        self._cursor += 1
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, dict):
            return a_response(item)
        if isinstance(item, FakeResponse):
            return item
        raise TypeError(f"FakeHttp 不支持的预设响应类型: {type(item).__name__}")

    def post(self, url: str, **kwargs) -> FakeResponse:
        return self._respond("POST", url, kwargs)

    def get(self, url: str, **kwargs) -> FakeResponse:
        return self._respond("GET", url, kwargs)

    def close(self) -> None:
        """无真实连接需要释放；提供它是为了能安全替换真实 session。"""

    def __enter__(self) -> "FakeHttp":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False


def a_response(
    json_body: dict | None = None, *, status: int = 200, text: str = ""
) -> FakeResponse:
    """构造一个响应。``json_body`` 给定时 ``text`` 自动序列化。"""
    return FakeResponse(status_code=status, text=text, json_body=json_body)


def _account_dict(row) -> dict:
    """把仓储读回的行整理成 dict（extra 已解析；JSON 后端下也是模型）。"""
    return {
        "id": row.id,
        "platform": row.platform,
        "email": row.email,
        "password": row.password,
        "user_id": row.user_id,
        "region": row.region,
        "token": row.token,
        "status": str(row.status or ""),
        "extra": row.get_extra(),
    }


def assert_account_persisted(
    email: str,
    *,
    platform: str = "chatgpt",
    expect_at: bool = True,
    expect_rt: bool = True,
) -> dict:
    """从仓储读回账号并断言凭证完整性，返回账号 dict（含 extra）。

    断言三件事：

    1. 账号真的落库了（按 ``(platform, email)`` 查得到）；
    2. 状态是正常态 ``registered``（线路成功却落下 banned/invalid → 抓）；
    3. ``expect_at`` / ``expect_rt`` 要求 extra 里有非空的 AT / RT（按凭证
       字段的规范名读取，自动认 camelCase 别名）。

    ``access_token_only`` 这类**本来就不要 RT** 的线路传 ``expect_rt=False``；
    别用它放过「要 RT 却没拿到」——那正是要抓的缺陷。
    """
    row = account_repository.find_by_email(platform, email)
    assert row is not None, f"账号未落库：{platform}/{email}"
    data = _account_dict(row)
    status = str(data["status"] or "").strip().lower()
    assert status == AccountStatus.REGISTERED.value, (
        f"账号状态不是 {AccountStatus.REGISTERED.value}：{platform}/{email} → {status!r}"
    )
    extra = data["extra"]
    at = get_credential(extra, "access_token")
    rt = get_credential(extra, "refresh_token")
    if expect_at:
        assert at, f"账号缺少 access_token（AT）：{platform}/{email}"
    if expect_rt:
        assert rt, f"账号缺少 refresh_token（RT）：{platform}/{email}"
    return data


def assert_no_secret_leak(text: str, secrets: list[str]) -> None:
    """断言 text 里不含任一 secret 的**完整值**（允许前缀/后缀出现）。

    只拦完整值：日志/响应里的掩码（``abc***``、``***123``）本来就是给排查
    用的，判成泄漏会把「打了掩码」的线路也弄红。空串元素跳过
    （``"" in text`` 恒真，会把所有线路都弄红）。
    """
    haystack = str(text or "")
    for index, secret in enumerate(secrets or []):
        value = str(secret or "")
        if not value:
            continue
        assert value not in haystack, (
            f"文本泄漏了完整凭证：secrets[{index}]（{len(value)} 字符）"
            # 不把命中内容回显出来：那等于把泄漏本体再打进一次测试日志
        )


#: Account 数据类接受的字段（其余键名一律拒绝，避免打错键静默丢失）。
_ACCOUNT_FIELDS = frozenset(
    {"password", "user_id", "region", "token", "status", "extra", "created_at"}
)

#: make_account 造过账号的平台 —— cleanup_accounts 要连它们一起扫（平台分库）。
_ACCOUNT_PLATFORMS: set[str] = set(SUPPORTED_PLATFORMS)


def make_account(platform: str = "chatgpt", **fields) -> dict:
    """在当前后端建一个账号（走 ``AccountRepository.upsert``），返回 dict。

    ``fields`` 只接受 ``Account`` 的字段：``email`` 必给；``password`` 缺省为
    空串；另有 ``user_id`` / ``region`` / ``token`` / ``status`` / ``extra`` /
    ``created_at``。平台自定义字段请放进 ``extra={...}`` —— 键名打错会在这里
    报错，不会静默丢弃。
    """
    email = str(fields.pop("email", "") or "")
    if not email:
        raise ValueError("make_account 需要 email（账号的唯一业务键）")
    unknown = sorted(set(fields) - _ACCOUNT_FIELDS)
    if unknown:
        raise ValueError(
            f"make_account 不认识的字段: {unknown}；自定义字段请放 extra={{...}}"
        )

    account = Account(
        platform=platform,
        email=email,
        password=str(fields.get("password") or ""),
        user_id=str(fields.get("user_id") or ""),
        region=str(fields.get("region") or ""),
        token=str(fields.get("token") or ""),
        status=fields.get("status") or AccountStatus.REGISTERED,
        extra=dict(fields.get("extra") or {}),
    )
    if fields.get("created_at") is not None:
        account.created_at = fields["created_at"]

    row = account_repository.upsert(account)
    # upsert 成功才登记平台：失败时没建出行，cleanup 不用扫
    _ACCOUNT_PLATFORMS.add(str(platform).strip().lower())
    return _account_dict(row)


def cleanup_accounts(*emails: str) -> None:
    """删掉指定邮箱的账号（测试收尾用）。

    仓储没有「按邮箱删」的接口，所以先按邮箱找行、再按 ``(id, platform)`` 删。
    平台扫描范围 = ``make_account`` 造过的平台 + 仓库实际支持的平台
    （``SUPPORTED_PLATFORMS``）——账号按平台分库，漏扫一个平台就会留下脏行，
    干扰后面测试的判重与计数。
    """
    platforms = sorted(_ACCOUNT_PLATFORMS)
    for email in emails:
        for platform in platforms:
            row = account_repository.find_by_email(platform, email)
            if row is not None and row.id is not None:
                account_repository.delete(row.id, platform=platform)