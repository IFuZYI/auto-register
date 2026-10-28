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

替身要「像真的」到什么程度
--------------------------
`FakeHttp`/`FakeResponse` 不是只有 `status_code` + `json()` 就够：真实线路
还会 `resp.raise_for_status()`、读 `resp.headers["Location"]`、翻
`session.cookies`。缺一样，线路就 `AttributeError`；cookie jar 缺失时 warmup
恒判失败并重建真实 session（``auth_flow.py:387``）—— 「无网络」的测试打真网络。
所以下面按**真实库的形状**补齐这几处最小接口。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from requests.exceptions import HTTPError

from core.base_platform import Account, AccountStatus
from core.credential_fields import get_credential, token_column_credential
from core.db import account_repository
from core.registry import SUPPORTED_PLATFORMS


class _FakeHeaders(dict):
    """大小写无关的响应头（``Location`` / ``location`` 都能读）。

    真实库（requests / curl_cffi）的 headers 是大小写不敏感的；线路代码
    两种拼写都出现过（``resp.headers.get("Location")`` 与 ``.get("location")``），
    用普通 dict 会让其中一半静默拿不到值 —— 重定向链会「看起来没跳」。
    """

    def __init__(self, headers: dict | None = None) -> None:
        # 注意：dict(mapping) 走 C 层更新、**不会**调用下面被覆写的 __setitem__，
        # 所以键必须在构造时自己归一（实测：直接 dict(...) 后 "Location" 就读不到）。
        super().__init__({str(k).lower(): v for k, v in (headers or {}).items()})

    def __getitem__(self, key: str):
        return super().__getitem__(str(key).lower())

    def __setitem__(self, key: str, value) -> None:
        super().__setitem__(str(key).lower(), value)

    def __contains__(self, key) -> bool:
        return super().__contains__(str(key).lower())

    def get(self, key, default=None):
        return super().get(str(key).lower(), default)

    def setdefault(self, key, default=None):
        return super().setdefault(str(key).lower(), default)


class FakeResponse:
    """一次 HTTP 响应的替身（线路代码实际会碰的成员都有）。

    只带 ``status_code`` / ``text`` / ``json()`` 时，真实线路会在两处直接炸：
    ``resp.raise_for_status()``（``get_auth_session`` 的必经路径）与
    ``resp.headers.get("Location")``（任何 3xx 分支）。
    """

    def __init__(
        self,
        status_code: int = 200,
        text: str = "",
        json_body: dict | None = None,
        *,
        headers: dict | None = None,
        url: str = "",
    ) -> None:
        self.status_code = status_code
        #: json_body 给定时 text 自动序列化，两个视图保持一致
        self.text = text if json_body is None else json.dumps(json_body, ensure_ascii=False)
        self.headers = _FakeHeaders(headers or {})
        #: 最终落点（线路读它判断落在哪一页：``getattr(resp, "url", "")``）
        self.url = url
        self._json_body = json_body

    def json(self) -> dict:
        """返回 JSON 体：给了 ``json_body`` 就用它，否则解析 ``text``（空体 → {}）。

        返回的是**深拷贝**：线路代码会就地改这个 dict（补字段、删键），
        直接交出内部对象会让「第二次读到的响应」被第一次调用污染。
        """
        if self._json_body is not None:
            return json.loads(json.dumps(self._json_body))
        return json.loads(self.text) if self.text else {}

    def raise_for_status(self) -> None:
        """4xx/5xx 抛 ``requests.HTTPError``（与真实响应同款）；2xx/3xx 不抛。

        3xx 不抛是有意的：重定向链要自己读 ``Location`` 手动跟随，
        ``allow_redirects=False`` 的响应本来就不该被当成错误。
        """
        if 400 <= int(self.status_code or 0):
            raise HTTPError(f"HTTP {self.status_code}", response=self)


@dataclass
class RecordedRequest:
    """``FakeHttp`` 记下的一次请求（供断言 URL / body / headers）。"""

    url: str
    method: str
    body: dict | str = ""
    headers: dict = field(default_factory=dict)


@dataclass
class _FakeCookie:
    """cookie jar 里的一条 —— 形状对齐真实库（``.name`` / ``.value`` / ``.domain``）。

    ``signup.py`` 与 ``trace.py`` 会 `for cookie in session.cookies` 并读属性，
    只实现 dict 接口不够。
    """

    name: str
    value: str
    domain: str = ".chatgpt.com"


class _FakeCookieJar:
    """最小 cookie jar：``get_dict`` / ``set`` / ``get`` / ``clear`` / 迭代。

    为什么必须有：warmup 的唯一成功判据是 ``session.cookies.get_dict()`` 里有
    ``oai-did``；没有 jar 时 warmup 恒 False，会重建 4 次真实 session
    （``auth_flow.py:387``）—— 「无网络」的测试反而打真网络。``signup.py`` 还会
    直接迭代 jar 取 cookie 名。
    """

    def __init__(self) -> None:
        self._items: dict[str, _FakeCookie] = {}

    def set(self, name: str, value: str, domain: str = ".chatgpt.com", **_) -> None:
        self._items[str(name)] = _FakeCookie(str(name), str(value), domain or ".chatgpt.com")

    def get(self, name: str, default=None, **_) -> str:
        item = self._items.get(str(name))
        return item.value if item is not None else default

    def get_dict(self, **_):
        return {item.name: item.value for item in self._items.values()}

    def clear(self, **_):
        self._items.clear()

    def __iter__(self):
        return iter(list(self._items.values()))

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, name) -> bool:
        return str(name) in self._items


class FakeHttp:
    """按顺序返回预设响应的 session 替身。

    用法::

        http = FakeHttp([a_response({"access_token": "at-1"}), OSError("boom")])
        with http as session:
            ...
        assert session.requests[0].url == "..."

    预设项可以是 `FakeResponse`、裸 dict（当作 JSON 体）、或异常实例（轮到它
    时抛出，用来模拟连接失败）。``cookies`` 预置 cookie jar（warmup 需要
    ``oai-did`` 才算成功，否则线路会重建 session 逃到网络）。

    **用尽即抛** `AssertionError`：响应不够时静默复用上一条，会让「线路多发了
    请求」这种缺陷隐身 —— 尤其是注册线路，多发的请求往往就是要修的 bug。
    """

    def __init__(
        self,
        responses: list[FakeResponse | dict | Exception],
        *,
        cookies: dict | None = None,
    ) -> None:
        self._responses = list(responses)
        self._cursor = 0
        #: 记录所有发出的请求（供断言 URL / body / headers）
        self.requests: list[RecordedRequest] = []
        #: 线路代码会直接读写它（warmup / signup / redirect 都靠 cookie 判状态）
        self.cookies = _FakeCookieJar()
        for name, value in (cookies or {}).items():
            self.cookies.set(name, value)

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
    json_body: dict | None = None,
    *,
    status: int = 200,
    text: str = "",
    headers: dict | None = None,
    url: str = "",
) -> FakeResponse:
    """构造一个响应。``json_body`` 给定时 ``text`` 自动序列化。"""
    return FakeResponse(
        status_code=status, text=text, json_body=json_body, headers=headers, url=url
    )


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


def _credential(row, extra: dict, platform: str, field_name: str) -> str:
    """按项目的**规范读法**取凭证：先 extra（含 camelCase 别名），再 token 列兜底。

    与 ``platforms/chatgpt/plugin.py`` 的读法逐字一致（``get_credential(...) or
    token_column_credential(row, platform, ...)``）。只读 extra 会漏掉「历史行 /
    导入行只写了 token 列」的账号 —— 明明有 AT，夹具却报「缺 AT」。
    """
    return get_credential(extra, field_name) or token_column_credential(
        row, platform, field_name
    )


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
    3. ``expect_at`` / ``expect_rt`` 要求 AT / RT 存在。读取走项目规范读法：
       ``extra``（自动认 camelCase 别名）+ ``token`` 列兜底 —— 与
       ``platforms/chatgpt/plugin.py`` 相同，不另立一套。

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
    at = _credential(row, extra, platform, "access_token")
    rt = _credential(row, extra, platform, "refresh_token")
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


def _as_text(value) -> str:
    """只在 None 时给空串：``0`` 是合法值（``str(0) == "0"``），不是空。

    用 ``value or ""`` 会把 ``0`` / ``False`` 这类合法值吞成空串。
    """
    return "" if value is None else str(value)


def _as_created_at(value):
    """把 ``created_at`` 归一成 datetime —— 仓储只认 datetime，其它类型会被丢。

    ``Account.created_at`` 数据类默认是 unix 秒（int），但
    ``merge_account_fields`` 仅在 ``isinstance(..., datetime)`` 时才落库。
    传 int 不报错、静默丢字段，是这里最容易踩的坑：所以数字自动转 UTC
    datetime，其余不认识的类型直接报错。
    """
    from datetime import datetime, timezone

    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    raise ValueError(
        f"make_account 的 created_at 需为 datetime 或 unix 秒，收到 {type(value).__name__}"
    )


def make_account(platform: str = "chatgpt", **fields) -> dict:
    """在当前后端建一个账号（走 ``AccountRepository.upsert``），返回 dict。

    ``fields`` 只接受 ``Account`` 的字段：``email`` 必给；``password`` 缺省为
    空串；另有 ``user_id`` / ``region`` / ``token`` / ``status`` / ``extra`` /
    ``created_at``（datetime 或 unix 秒，都会真的落库）。平台自定义字段请放进
    ``extra={...}`` —— 键名打错会在这里报错，不会静默丢弃。
    """
    email = _as_text(fields.pop("email", ""))
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
        password=_as_text(fields.get("password")),
        user_id=_as_text(fields.get("user_id")),
        region=_as_text(fields.get("region")),
        token=_as_text(fields.get("token")),
        status=fields.get("status") or AccountStatus.REGISTERED,
        extra=dict(fields.get("extra") or {}),
    )
    created_at = _as_created_at(fields.get("created_at"))
    if created_at is not None:
        account.created_at = created_at

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
