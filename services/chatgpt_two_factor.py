"""给库里已有的 ChatGPT 账号补绑 TOTP 2FA。

和补 RT 一样按代价从低到高试两条路：

    ① 会话复用：拿库里的 session_token/access_token 恢复登录态直接 enroll。
       不发邮件、不碰密码页、不跑 PoW，几秒钟完事。会话还活着的号都走这条。

    ② 协议重登：会话过期时才走。邮箱 + 密码重跑一遍登录正式链再 enroll，
       要一次 PoW，多半还要收一封验证码邮件。

密钥只在 enroll 响应里下发一次，服务端取不回。所以拿到就往 ``extra_json`` 里
写，写不进去这个号的 2FA 等于废了。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from sqlmodel import Session, select

from core.base_platform import resolve_mailbox_otp_timeout
from core.db import AccountModel
from platforms.chatgpt.device_id import resolve_device_id
from platforms.chatgpt.protocol import AuthFlow, Config
from platforms.chatgpt.protocol.two_factor import (
    TwoFactorBindResult,
    bind_totp_inline,
    bind_totp_via_login,
)
from platforms.chatgpt.protocol_log_relay import mirror_protocol_logs
from services.chatgpt_account_selection import select_chatgpt_accounts

logger = logging.getLogger(__name__)


def account_totp_secret(model: AccountModel) -> str:
    return str(model.get_extra().get("totp_secret") or "").strip()


def account_missing_two_factor(model: AccountModel) -> bool:
    return not account_totp_secret(model)


def select_two_factor_targets(
    session: Session,
    *,
    account_ids: Optional[Iterable[int]] = None,
    all_filtered: bool = False,
    email: str = "",
    status: str = "",
    plus_status: str = "",
    created_at_start: Optional[datetime] = None,
    created_at_end: Optional[datetime] = None,
    only_missing_2fa: bool = True,
) -> tuple[list[AccountModel], list[int]]:
    """挑出要绑 2FA 的号，返回 ``(账号列表, 找不到的 id)``。

    ``only_missing_2fa`` 是默认行为：库里已经有密钥的号再 enroll 一遍只会把用户
    手上的验证器废掉。手动对单个号操作时可以关掉，让它把"已绑"这个结论也跑出来。

    `created_at_start/end` 与列表接口的日期筛选同口径（显示与执行一致）。
    """
    return select_chatgpt_accounts(
        session,
        account_ids=account_ids,
        all_filtered=all_filtered,
        email=email,
        status=status,
        plus_status=plus_status,
        created_at_start=created_at_start,
        created_at_end=created_at_end,
        keep=account_missing_two_factor if only_missing_2fa else None,
    )


def bind_account_two_factor(
    *,
    email: str,
    password: str = "",
    extra: Optional[dict] = None,
    token: str = "",
    config: Optional[dict] = None,
    proxy: Optional[str] = None,
    allow_login: bool = True,
    log_fn: Optional[Callable[[str], None]] = None,
    task_control=None,
    attempt_id=None,
    persist_secret: Optional[Callable[[str], None]] = None,
) -> TwoFactorBindResult:
    """按账号字段绑 2FA，不落库（落库交给 ``build_extra_patch``）。

    只收纯数据不收 ORM 对象：这一趟要跑几十秒网络请求，调用方得以在这期间把
    数据库连接还回池子里。

    ``persist_secret`` 是**密钥一到手就落库**的回调（写前日志）：密钥只在 enroll
    响应里下发一次，从「拿到」到「调用方按 ``build_extra_patch`` 落库」之间隔着
    activate + 复核两次网络往返 —— 中间进程被 kill、任务被中断、或调用方漏了
    落库，这个号的 2FA 就永久锁死（实测踩过）。给了回调的话，密钥在 enroll 返回
    的那一刻就先写进库，后续步骤再出问题也不会丢。
    """
    extra = dict(extra or {})
    config = dict(config or _load_config())
    log = log_fn or logger.info

    existing = str(extra.get("totp_secret") or "").strip()
    if existing:
        return TwoFactorBindResult(
            already_bound=True, secret=existing, error_message="库里已经有这个号的 TOTP 密钥"
        )

    from core.credential_fields import get_credential

    session_token = get_credential(extra, "session_token")
    access_token = get_credential(extra, "access_token") or token or ""
    # 设备标识复用：字段空时回退 cookies 里的 oai-did（存量账号的原始设备）
    device_id = resolve_device_id(extra)

    result = TwoFactorBindResult(error_message="没有可用的绑定路径")
    if session_token or access_token:
        with mirror_protocol_logs(log_fn):
            result = _bind_via_session(
                email=email,
                session_token=session_token,
                access_token=access_token,
                device_id=device_id,
                proxy=proxy,
                log=log,
                persist_secret=persist_secret,
            )
        if result.ok or result.already_bound:
            return result
        log(f"[绑2FA] 复用会话未果：{result.summary()}")
    else:
        log("[绑2FA] 库里没有 session_token / access_token，跳过会话复用")

    if not allow_login:
        return TwoFactorBindResult(error_message=f"{result.summary()}；已关闭协议重登")
    if not password:
        return TwoFactorBindResult(error_message=f"{result.summary()}；库里没有密码，无法协议重登")

    log(f"[绑2FA] 改走协议重登: {email}")
    mail_provider = _resolve_mail_provider(
        email,
        extra=extra,
        config=config,
        proxy=proxy,
        log=log,
        task_control=task_control,
        attempt_id=attempt_id,
    )
    with mirror_protocol_logs(log_fn):
        return bind_totp_via_login(
            Config(proxy=(proxy or "").strip() or None),
            email,
            password,
            mail_provider=mail_provider,
            env_overrides={"OTP_TIMEOUT": str(_otp_timeout(config)), "WEBUI_ALLOW_LOGIN": "1"},
            on_secret=persist_secret,
            # 设备标识复用：重登沿用库里存的 oai-did（`device_id` 在上面已
            # 从 extra 读出，会话复用那条路也用它）。
            device_id=device_id,
        )


def build_extra_patch(result: TwoFactorBindResult) -> dict[str, Any]:
    """把绑定结果整理成可以合并进 ``extra_json`` 的补丁。

    没拿到密钥就只留一条留痕：用空串覆盖掉库里原有的 totp_secret 等于把号的
    2FA 弄成永久锁死状态。
    """
    patch: dict[str, Any] = {}
    if result.secret:
        patch["totp_secret"] = result.secret
    # 设备标识：慢路径重登沿用/收敛到的 oai-did，落库供后续复用
    device_id = str(getattr(result, "device_id", "") or "").strip()
    if device_id:
        patch["device_id"] = device_id
    patch["chatgpt_2fa"] = {
        "bound": result.ok or result.already_bound,
        "message": result.summary(),
        "at": datetime.now(timezone.utc).isoformat(),
    }
    return patch


def apply_two_factor_result(
    model: AccountModel,
    result: TwoFactorBindResult,
    *,
    session: Optional[Session] = None,
    commit: bool = False,
) -> dict[str, Any]:
    """把绑定结果落到账号行上，返回实际写入的补丁。

    ``result.banned`` 为真时同时把状态落成「禁用」—— 用户要求禁用靠登录
    流程发掘：绑 2FA 重登撞上「号没了」的措辞，正是这个结论的一手来源。
    """
    patch = build_extra_patch(result)
    extra = model.get_extra()
    extra.update(patch)
    model.set_extra(extra)
    if result.banned:
        from services.chatgpt_account_state import apply_chatgpt_status_policy

        apply_chatgpt_status_policy(model, banned=True)
    model.updated_at = datetime.now(timezone.utc)
    if session is not None:
        session.add(model)
        if commit:
            session.commit()
    return patch


def _totp_journal_dir() -> "Path":
    from core.paths import SECRETS_DIR

    d = SECRETS_DIR / "totp_journal"
    d.mkdir(parents=True, exist_ok=True)
    # 里面是明文密钥：目录 0700、文件 0600（同目录下的 credential_key 也是这个待遇）
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d


def _journal_path(email: str) -> "Path":
    import hashlib

    digest = hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:16]
    return _totp_journal_dir() / f"{digest}.json"


def _write_secret_journal(email: str, secret: str) -> None:
    """把密钥写进写前日志（原子替换 + fsync）。

    DB 写不一定成功：动作链在绑定期间一直占着一条 SQLAlchemy session，等它
    中间 flush 过写事务，另开连接去写就是 `database is locked`（实测复现）。
    而这条路径的意义恰恰是「进程被杀/任务被停也要保住密钥」—— 只赌 DB 写等于
    没做。文件先落地，DB 写失败时启动恢复会把它补回去。
    """
    import json
    import os
    import tempfile

    path = _journal_path(email)
    payload = {
        "email": email,
        "totp_secret": secret,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        # mkstemp 已经是 0600，显式再钉一次防 umask 影响
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _journal_is_stale(payload: dict, *, max_age_days: int = 14) -> bool:
    """日志条目是不是老得不该再自动补写了。

    邮箱是会被释放、重新注册成另一个号的 —— 把上一个号的密钥写进新号等于给
    它塞一把用不上的验证器，还会掩盖「这个号本来就没绑」。给个 14 天的窗口：
    正常崩溃重启是分钟级，够用；真超期的保留文件、报一行、等人处理。
    """
    raw = str(payload.get("at") or "").strip()
    if not raw:
        return False
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).days >= max_age_days


def recover_pending_totp_secrets(*, log: Optional[Callable[[str], None]] = None) -> int:
    """把写前日志里还没进库的密钥补写进库，返回补写条数。

    启动时调用一次（见 ``main.lifespan``）：绑定中途进程被杀、或 DB 写撞上
    `database is locked`，密钥就只在这份文件里 —— 不补回去的话号的 2FA 等于
    永久锁死（服务端不下发第二次）。

    账号行还不存在的条目**保留文件**（不删）：注册链是「先拿到密钥、账号稍后
    才落库」，此时删掉等于把唯一的副本扔了 —— 留着下次启动再试，也可人工查。
    """
    import json

    try:
        directory = _totp_journal_dir()
    except Exception:  # noqa: BLE001 - 恢复失败不该挡住启动
        return 0

    applied = 0
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            email = str(payload.get("email") or "").strip()
            secret = str(payload.get("totp_secret") or "").strip()
            if not email or not secret:
                path.unlink(missing_ok=True)
                continue
            # 超期的条目不再自动补写：邮箱可能已被释放、重新注册成了另一个号，
            # 把旧密钥写进新号等于给它塞一把用不上的验证器。文件保留供人工核对。
            if _journal_is_stale(payload):
                if log is not None:
                    log(f"[2FA] 写前日志已超期，未自动补写（待人工核对）: {email}")
                continue
            outcome = _apply_journal_entry(email, secret)
            if outcome == "missing":
                # 账号还没有行：留着，等它建出来再补（注册链的正常时序）
                if log is not None:
                    log(f"[2FA] 写前日志待补（账号尚未落库）: {email}")
                continue
            if outcome == "applied":
                applied += 1
                if log is not None:
                    log(f"[2FA] 写前日志补写密钥: {email}")
            if outcome == "conflict":
                # 库里已有一把**不同**的密钥：删掉等于把这一把永久扔了
                # （服务端不下发第二次）。留着让人工裁决，只报一条。
                if log is not None:
                    log(f"[2FA] 写前日志与库里的密钥不一致，已保留文件待人工核对: {email}")
                continue
            # applied / duplicate → 都已入库，日志文件可以清掉
            path.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响其它条
            if log is not None:
                log(f"[2FA] 写前日志恢复失败 {path.name}: {exc}")
    return applied


def _apply_journal_entry(email: str, secret: str) -> str:
    """把一条日志里的密钥写进库。

    返回 ``"applied"``（已写）/ ``"duplicate"``（库里是同一把，无需写）/
    ``"conflict"``（库里是**另一把**，没动它）/ ``"missing"``（账号行还不存在）。
    调用方据此决定能不能删日志文件 —— 只有前两种能删。
    """
    from core.db import platform_session

    with platform_session("chatgpt") as s:
        row = s.exec(select(AccountModel).where(AccountModel.email == email)).first()
        if row is None:
            return "missing"
        extra = row.get_extra()
        existing = str(extra.get("totp_secret") or "").strip()
        if existing:
            return "duplicate" if existing == secret.strip() else "conflict"
        extra["totp_secret"] = secret
        row.set_extra(extra)
        row.updated_at = datetime.now(timezone.utc)
        s.add(row)
        s.commit()
    return "applied"


def make_secret_persister(
    account_id: Optional[int] = None,
    *,
    email: Optional[str] = None,
    log: Optional[Callable[[str], None]] = None,
) -> Callable[[str], None]:
    """造一个「密钥一到手就落库」的回调（写前日志）。

    给 ``bind_account_two_factor(persist_secret=...)`` 用。先写文件日志（原子
    替换 + fsync），再写库；自己开一个短连接写完就关 —— 绑 2FA 那趟要跑几十秒
    网络请求，不能占着连接；而这一步必须**立刻**做，不能等外层那趟跑完（实测
    踩过：密钥拿到后外层还没落库就出问题，密钥永久丢失、号的 2FA 锁死）。

    定位账号给 ``account_id`` 或 ``email`` 其一（任务链有 id，平台动作链只有
    email）。只在库里还没有密钥时写（不覆盖已有的）。

    **文件日志先于 DB 落地**：动作链绑定期间占着一条 session，等它 flush 过写
    事务后另开连接写库会 `database is locked`（实测复现），而这条路径的意义就是
    「进程被杀也要保住密钥」。文件写成功即视为安全 —— DB 写失败只记 warning，
    启动时 ``recover_pending_totp_secrets`` 会补写；文件也写不成功才往上抛。
    """
    if account_id is None and not email:
        raise ValueError("make_secret_persister 需要 account_id 或 email")

    def _persist(secret: str) -> None:
        value = str(secret or "").strip()
        if not value:
            return
        from core.db import platform_session

        target_email = str(email or "").strip()
        row = None
        try:
            with platform_session("chatgpt") as s:
                if account_id is not None:
                    row = s.get(AccountModel, int(account_id))
                else:
                    row = s.exec(
                        select(AccountModel).where(AccountModel.email == target_email)
                    ).first()
                if row is None:
                    raise RuntimeError(f"账号 {account_id or target_email} 不存在，密钥无法落库")
                target_email = str(row.email or target_email)
        except Exception:
            # 读不到就先用传入的 email 记账（任务链两条都有）
            if not target_email:
                raise

        # 1) 写前日志先落地 —— 这一步成功密钥就不会丢了
        _write_secret_journal(target_email, value)

        # 2) 再写库；撞锁（外层 session 占着）不算失败，启动恢复会补
        try:
            outcome = _apply_journal_entry(target_email, value)
        except Exception as exc:  # noqa: BLE001 - 密钥已在文件日志里
            if log is not None:
                log(f"[绑2FA] 密钥已写入写前日志（DB 暂写不进去，启动时补写）: {exc}")
            return
        if outcome == "applied":
            _journal_path(target_email).unlink(missing_ok=True)
            if log is not None:
                log("[绑2FA] 密钥已到手，已先落库（后续步骤失败也不会丢）")
        elif outcome == "duplicate":
            _journal_path(target_email).unlink(missing_ok=True)
        elif outcome == "conflict":
            # 库里是另一把密钥：不动它，也留着日志文件待人工核对
            if log is not None:
                log("[绑2FA] 库里已有另一把密钥，日志文件已保留待人工核对")
        else:
            # 账号行还没有（注册链：号稍后才落库）→ 留文件，启动时补
            if log is not None:
                log("[绑2FA] 密钥已写入写前日志（账号尚未落库，启动时补写）")

    return _persist


def _bind_via_session(
    *,
    email: str,
    session_token: str,
    access_token: str,
    device_id: str,
    proxy: Optional[str],
    log: Callable[[str], None],
    persist_secret: Optional[Callable[[str], None]] = None,
) -> TwoFactorBindResult:
    log(f"[绑2FA] 尝试复用已有会话: {email}")
    try:
        flow = AuthFlow(Config(proxy=(proxy or "").strip() or None))
        flow.from_existing_credentials(session_token, access_token, device_id)
    except Exception as exc:  # noqa: BLE001
        return TwoFactorBindResult(error_message=f"恢复会话失败: {exc}")
    if not flow.result.access_token:
        return TwoFactorBindResult(error_message="库里的 session/access token 已失效")
    return bind_totp_inline(flow, flow.result.access_token, on_secret=persist_secret)


def _resolve_mail_provider(
    email: str,
    *,
    extra: dict,
    config: dict,
    proxy: Optional[str],
    log: Callable[[str], None],
    task_control,
    attempt_id,
):
    from services.chatgpt_otp_mailbox import resolve_otp_mail_provider

    mail_provider, reason = resolve_otp_mail_provider(
        email,
        account_extra=extra,
        config=config,
        proxy=proxy,
        log_fn=log,
        task_control=task_control,
        attempt_id=attempt_id,
    )
    if mail_provider is None:
        log(f"[绑2FA] {email} 暂时读不到收件箱（{reason}），需要邮箱验证码时会失败")
    else:
        log(f"[绑2FA] 收件通道: {getattr(mail_provider, 'display_name', '邮箱')} → {email}")
    return mail_provider


def _otp_timeout(config: dict) -> int:
    return resolve_mailbox_otp_timeout(config)


def _load_config() -> dict:
    from core.config_store import config_store

    return config_store.get_all() or {}
