"""    from services import task_service as _api  # 延迟 import：patch 打在 task_service 上必须被看到
注册 / 回填 RT / 绑定 2FA 的任务执行实现（原 api/tasks.py 的 470–1165 行）。

⚠️ 本模块的函数大量调用 `api/tasks.py` 里的辅助（`_api._log`/`_api._save_task_log`/
`_api.task_store`/`_api._persist_task_snapshot`/`_api._account_already_registered`/
`_api._auto_upload_integrations`/`_api.normalize_register_retry_times`/`_api.MAX_DEAD_END_ROUNDS`），
统一在**函数内** `from services import task_service as _api` 取门面 —— 这样
`patch("api.tasks._save_task_log")` 才能生效。这是全仓唯一需要函数内 import 的
地方：api.tasks → 本模块（模块级）与本模块 → api.tasks（函数级）构成单向循环。
"""
from __future__ import annotations


def _redact_proxy(value: object) -> str:
    """日志里的代理串一律打码（凭据不进日志/接口/截图）。"""
    from core.proxy_utils import redact_proxy_url

    return redact_proxy_url(str(value or ""))


import logging

logger = logging.getLogger(__name__)

from services.task_store_io import persist_task_snapshot

import threading
import time
from typing import TYPE_CHECKING, Callable, Optional

from sqlmodel import Session

from core.db import account_repository, current_engine
from core.environment import EnvironmentNotReadyError, is_environment_error_message
from core.task_runtime import (
    AttemptOutcome,
    AttemptResult,
    NonRetryableRegisterError,
    SkipCurrentAttemptRequested,
    StopTaskRequested,
)

if TYPE_CHECKING:  # 仅类型标注用（from __future__ import annotations 下运行时不需要）
    from api.tasks import (  # noqa: F401
        BackfillRtTaskRequest,
        BatchActionTaskRequest,
        Bind2faTaskRequest,
        ProbeStatusTaskRequest,
        RecoverAccountsTaskRequest,
        RefreshTokenTaskRequest,
        RegisterTaskRequest,
    )


#: Grok 出口预检最多换几个出口。5 个够用了：池子里真有能用的，前几个就会中；
#: 全都不行说明这批出口整体被标记了，再换也是白烧时间。
_GROK_EXIT_PROBE_TRIES = 5


def _truthy_extra(value: object, default: bool) -> bool:
    if value is None or value == "":
        return default
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def _pick_usable_exit(
    proxy: str, *, task_id: str, extra: dict, log, get_next, report_bad, normalize
) -> str:
    """Grok 注册前先探出口，别拿 iCloud 别名去撞一堵已知会撞的墙。

    `account:email-signup-unavailable` 跟**出口 IP 声誉**走、不跟邮箱走
    （`platforms/grok/signup_pages.py` 顶部的实测结论）。而预检打的是同一个
    接口、用的是永远收不到信的 `@example.invalid`，所以：

    - 被拒 → 这个出口别用了，`report_proxy_error` 摘掉，换下一个；
    - 没被拒 / 预检本身没跑成 → 放行（fail-open，预检不该变成新的失败点）。

    关掉用 `grok_exit_preflight=0`。
    """
    if not _truthy_extra(extra.get("grok_exit_preflight"), True):
        return proxy
    from platforms.grok.exit_probe import probe_exit

    current = proxy
    for attempt in range(_GROK_EXIT_PROBE_TRIES):
        if not current:
            return ""
        result = probe_exit(current)
        if result.get("ok"):
            if attempt:
                log(task_id, f"出口预检：换到第 {attempt + 1} 个出口才通过")
            return current
        log(task_id, f"出口预检：{_redact_proxy(current)} 不可注册，换下一个")
        try:
            report_bad(current)
        except Exception:
            logger.debug("预检摘除出口失败", exc_info=True)
        current = normalize(get_next())
    log(task_id, f"出口预检：连试 {_GROK_EXIT_PROBE_TRIES} 个出口都不可注册，本轮放弃")
    return ""


def _run_register(task_id: str, req: RegisterTaskRequest):
    from services import task_service as _api  # 延迟 import：patch 打在 task_service 上必须被看到
    from core.registry import get
    from core.base_platform import RegisterConfig
    from core.db import save_account
    from modules.config import RegistrationContextBuilder
    from modules.mail import create_mailbox
    from core.proxy_utils import normalize_proxy_url

    control = _api.task_store.control_for(task_id)
    _api.task_store.mark_running(task_id)
    persist_task_snapshot(task_id)
    success = 0
    skipped = 0
    errors = []
    start_gate_lock = threading.Lock()
    next_start_time = time.time()

    def _sleep_with_control(
        wait_seconds: float,
        *,
        attempt_id: int | None = None,
    ) -> None:
        remaining = max(float(wait_seconds or 0), 0.0)
        while remaining > 0:
            control.checkpoint(attempt_id=attempt_id)
            chunk = min(0.25, remaining)
            time.sleep(chunk)
            remaining -= chunk

    try:
        PlatformCls = get(req.platform)

        # 任务级环境预检：环境不就绪（如 camoufox 浏览器没装/与包版本不配对）
        # 在分配邮箱/代理**之前**终止整个任务 —— 否则每个账号白跑一遍、重试
        # 轮全烧在同一个环境错误上（实测事故：task_1791300512055 两轮 3 秒
        # 全挂在 CamoufoxNotInstalled，还白领了别名）。平台没实现预检时默认放行。
        preflight = getattr(PlatformCls, "check_environment", None)
        if callable(preflight):
            verdict = preflight()
            if not verdict.ok:
                _api.log(
                    task_id,
                    f"[环境预检] 环境未就绪，任务终止（重开一轮同样结局）：{verdict.message}",
                )
                raise EnvironmentNotReadyError(verdict.message)

        # 预先计算 merged_extra，所有线程共享只读副本，避免每线程重复调用 config_store
        from core.config_store import config_store as _cs
        registration_context = RegistrationContextBuilder(_cs.get_all()).build(
            RegisterConfig(
                executor_type=req.executor_type,
                captcha_solver=req.captcha_solver,
                proxy=req.proxy,
                extra=req.extra,
            )
        )
        _base_extra = registration_context.settings

        # 批量预取代理（无固定代理时），减少每线程单独查 DB
        from core.proxy_pool import proxy_pool as _proxy_pool
        _prefetched_proxies: list[str] = []
        _prefetch_lock = threading.Lock()
        if not req.proxy and req.count > 1:
            with Session(current_engine()) as _s:
                from core.db import ProxyModel
                from core.models.proxy import TAG_REGISTER, parse_proxy_tags
                from sqlmodel import select as _sel
                _active = _s.exec(
                    _sel(ProxyModel).where(ProxyModel.is_active == True)
                ).all()
                # 注册链路优先用带 `register` 标签的出口；一个都没有时回退到
                # 全部活跃代理（存量部署升级上来时标签是空的，硬过滤会让注册
                # 任务直接「没有可用代理」）。
                _tagged = [
                    p.url
                    for p in _active
                    if p.url and TAG_REGISTER in parse_proxy_tags(getattr(p, "tags", ""))
                ]
                _prefetched_proxies = _tagged or [p.url for p in _active if p.url]

        def _get_proxy() -> Optional[str]:
            if req.proxy:
                return req.proxy
            if _prefetched_proxies:
                with _prefetch_lock:
                    if _prefetched_proxies:
                        import random
                        return random.choice(_prefetched_proxies)
            from core.models.proxy import TAG_REGISTER

            return _proxy_pool.get_next(tag=TAG_REGISTER)

        def _build_mailbox(proxy: Optional[str]):
            # 不给兜底 provider：用户要求默认留空、必须显式选。空串会让
            # create_mailbox 抛「未知邮箱提供商」并列出可用渠道，比悄悄用某个
            # 号池更安全。
            return create_mailbox(
                provider=_base_extra.get("mail_provider", ""),
                extra=_base_extra,
                proxy=proxy,
            )

        retry_times = _api.normalize_register_retry_times(req.register_retry_times)
        total_rounds = 1 + retry_times
        if total_rounds > 1:
            _api.log(
                task_id,
                f"失败重试轮数 {retry_times}：每个账号失败后最多重开 {retry_times} 轮，"
                f"连同首轮共 {total_rounds} 轮（每轮都是全新的代理/邮箱/号码/会话）",
            )

        def _is_dead_end(result: AttemptResult) -> bool:
            return result.outcome == AttemptOutcome.FAILED and not result.retryable

        def _do_one(i: int):
            """一个序号的完整交付：失败就整流程重开一轮，直到轮次用尽。

            重开的是整条链（新代理、新邮箱/号码、新会话），不是接码层的换号 ——
            半路建出来的号已经被占了，拿它死磕只会一直撞同一堵墙。
            """
            result = _do_one_round(i, 1, total_rounds)
            dead_end_rounds = 1 if _is_dead_end(result) else 0
            for round_no in range(2, total_rounds + 1):
                if result.outcome != AttemptOutcome.FAILED:
                    break
                if control.is_stop_requested():
                    break
                if dead_end_rounds >= _api.MAX_DEAD_END_ROUNDS:
                    _api.log(
                        task_id,
                        f"[RETRY] 第 {i + 1} 个账号连续 {dead_end_rounds} 轮都栽在"
                        f"「重开也是同样结局」的失败上，剩下 "
                        f"{total_rounds - round_no + 1} 轮不再重开"
                        f"（再开只会多几个没人认领的号）: {result.message}",
                    )
                    break
                _api.log(
                    task_id,
                    f"[RETRY] 第 {i + 1} 个账号第 {round_no - 1}/{total_rounds} 轮失败，"
                    f"开始第 {round_no}/{total_rounds} 轮重试（全新会话）: {result.message}",
                )
                result = _do_one_round(i, round_no, total_rounds)
                dead_end_rounds = dead_end_rounds + 1 if _is_dead_end(result) else 0
            if result.outcome == AttemptOutcome.FAILED:
                # 注册记录按"一个序号一条结果"记，所以只有跑完所有轮次才落一条
                # failed；否则重试成功了还会在统计里留下一条失败
                _api.save_task_log(
                    req.platform,
                    result.email,
                    "failed",
                    error=result.message,
                )
            return result

        def _do_one_round(i: int, round_no: int, rounds: int):
            nonlocal next_start_time
            _proxy = None
            current_email = req.email or ""
            attempt_id: int | None = None
            round_suffix = f"（第 {round_no}/{rounds} 轮）" if rounds > 1 else ""
            try:
                control.checkpoint()
                attempt_id = control.start_attempt()
                control.checkpoint(attempt_id=attempt_id)
                _proxy = normalize_proxy_url(_get_proxy())
                # Grok：先花 2 秒探出口，比「领别名 → 起浏览器 → 填表 → 被拒」
                # （半分钟 + 一个别名）便宜得多。
                if getattr(req, "platform", "") == "grok":
                    _proxy = _pick_usable_exit(
                        _proxy,
                        task_id=task_id,
                        extra=_base_extra,
                        log=_api.log,
                        get_next=_get_proxy,
                        report_bad=_proxy_pool.report_proxy_error,
                        normalize=normalize_proxy_url,
                    )
                if req.register_delay_seconds > 0:
                    with start_gate_lock:
                        control.checkpoint(attempt_id=attempt_id)
                        now = time.time()
                        wait_seconds = max(0.0, next_start_time - now)
                        if wait_seconds > 0:
                            _api.log(
                                task_id,
                                f"第 {i + 1} 个账号启动前延迟 {wait_seconds:g} 秒",
                            )
                            _sleep_with_control(
                                wait_seconds,
                                attempt_id=attempt_id,
                            )
                        next_start_time = time.time() + req.register_delay_seconds
                control.checkpoint(attempt_id=attempt_id)

                merged_extra = _base_extra

                _config = RegisterConfig(
                    executor_type=req.executor_type,
                    captcha_solver=req.captcha_solver,
                    proxy=_proxy,
                    extra=merged_extra,
                )
                # 自带邮箱的平台（iCloud 隐私邮箱）不建外部邮箱池：建了也不会被
                # 用到，还会因为全局默认渠道（`mail_provider`，默认留空）报
                # 「未配置邮箱服务」而直接失败。
                if getattr(PlatformCls, "uses_mailbox", True):
                    _mailbox = _build_mailbox(_proxy)
                else:
                    _mailbox = None
                _platform = PlatformCls(config=_config, mailbox=_mailbox)
                _platform._task_attempt_token = attempt_id
                _platform._log_fn = lambda msg: _api.log(task_id, msg)
                _platform.bind_task_control(control)
                if getattr(_platform, "mailbox", None) is not None:
                    _platform.mailbox._task_attempt_token = attempt_id
                    _platform.mailbox._log_fn = _platform._log_fn
                    # 告诉邮箱渠道「谁在用」—— 邮箱的消耗按平台记账：
                    # 同一个地址注册过 ChatGPT 之后还能注册 Grok。不注入的话
                    # 渠道退回旧的全局行为，会把地址对所有平台一起锁死。
                    _platform.mailbox._platform_name = req.platform
                _api.task_store.set_progress(task_id, f"{i + 1}/{req.count}")
                persist_task_snapshot(task_id)
                _api.log(task_id, f"开始注册第 {i + 1}/{req.count} 个账号{round_suffix}")
                if _proxy:
                    # 代理串里带账号密码，日志会被 `/api/tasks/{id}/logs` 原样回吐
                    # （安全审查 2026-10-30 F4）。日志只需要知道「走的是哪个出口」，
                    # 用与 API 同一套打码，主机:端口足够定位问题。
                    _api.log(task_id, f"使用代理: {_redact_proxy(_proxy)}")

                # 已注册的邮箱不重复注册：邮箱是账号唯一业务键，
                # 指定邮箱时先判重，命中就跳过（省掉整轮注册与验证码）。
                if req.email and _api.account_already_registered(req.platform, req.email):
                    _api.log(task_id, f"[SKIP] 该邮箱已注册，跳过: {req.email}")
                    return AttemptResult.skipped(f"邮箱已注册: {req.email}")

                account = _platform.register(
                    email=req.email or None,
                    password=req.password,
                )
                current_email = account.email or current_email
                if isinstance(account.extra, dict):
                    mail_provider = merged_extra.get("mail_provider", "")
                    if mail_provider:
                        account.extra.setdefault("mail_provider", mail_provider)
                    # 记下**注册时用的代理**：之后复用这个账号（测活、补 RT、
                    # 绑 2FA…）时优先回到同一个出口 IP —— 同一账号反复换 IP
                    # 容易被上游当成异常登录。代理不可用时会被换成新的并
                    # 覆盖这个字段（见 `proxy_pool.resolve_for_account`）。
                    #
                    # 注册时没走代理（`_proxy` 为空，如池子当时是空的）就**不写**：
                    # 不写空值，让复用路径的 resolve 知道「还没绑定」并补上 ——
                    # 绑定不再只发生在注册这一刻（用户要求）。
                    if _proxy:
                        account.extra["register_proxy"] = _proxy
                saved_account = save_account(account)
                if _proxy:
                    _proxy_pool.report_success(_proxy)
                _api.log(task_id, f"[OK] 注册成功: {account.email}")
                _api.save_task_log(req.platform, account.email, "success")
                _api.auto_upload_integrations(task_id, saved_account or account)
                cashier_url = (account.extra or {}).get("cashier_url", "")
                if cashier_url:
                    _api.log(task_id, f"  [升级链接] {cashier_url}")
                    _api.task_store.add_cashier_url(task_id, cashier_url)
                    persist_task_snapshot(task_id)
                return AttemptResult.success()
            except SkipCurrentAttemptRequested as e:
                _api.log(task_id, f"[SKIP] 已跳过当前账号: {e}")
                _api.save_task_log(
                    req.platform,
                    current_email,
                    "skipped",
                    error=str(e),
                )
                return AttemptResult.skipped(str(e))
            except StopTaskRequested as e:
                _api.log(task_id, f"[STOP] {e}")
                return AttemptResult.stopped(str(e))
            except Exception as e:
                if _proxy:
                    from core.proxy_utils import looks_like_proxy_error

                    if looks_like_proxy_error(e):
                        # 注册用的是**注册标签**的出口，这里摘掉它，下一轮
                        # `_get_proxy()` 会从剩下的里重新挑一个。
                        _proxy_pool.report_proxy_error(_proxy)
                        _api.log(
                            task_id,
                            f"代理异常，已摘除 {_proxy}，下一轮换出口",
                        )
                    else:
                        _proxy_pool.report_fail(_proxy)
                _api.log(task_id, f"[FAIL] 注册失败{round_suffix}: {e}")
                # 环境类错误（浏览器/Node 缺失等）即使以字符串形态传上来
                # （register_browser 的兜底 except 会把异常文本化）也判成
                # 不可重试 —— 重开一轮是同样的结局。
                non_retryable = isinstance(e, NonRetryableRegisterError) or (
                    is_environment_error_message(e)
                )
                return AttemptResult.failed(
                    str(e),
                    retryable=not non_retryable,
                    email=current_email,
                )
            finally:
                control.finish_attempt(attempt_id)

        from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed

        max_workers = min(req.concurrency, req.count)
        stopped = False
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_do_one, i) for i in range(req.count)]
            for f in as_completed(futures):
                try:
                    result = f.result()
                except CancelledError:
                    continue
                except Exception as e:
                    _api.log(task_id, f"[ERROR] 任务线程异常: {e}")
                    errors.append(str(e))
                    continue
                if result.outcome == AttemptOutcome.SUCCESS:
                    success += 1
                elif result.outcome == AttemptOutcome.SKIPPED:
                    skipped += 1
                elif result.outcome == AttemptOutcome.STOPPED:
                    stopped = True
                else:
                    errors.append(result.message)
                _api.task_store.update_counters(
                    task_id,
                    success=success,
                    registered=success + skipped + len(errors),
                )
                persist_task_snapshot(task_id)
                if stopped or control.is_stop_requested():
                    stopped = True
                    for pending in futures:
                        if pending is not f:
                            pending.cancel()
    except Exception as e:
        # 收尾路径自己绝不能再抛：这里本来就在处理异常，若 _api._log 再抛一次，
        # finish() 会被整个跳过，任务永远停在 running —— 实测过的
        # `[Errno 32] Broken pipe` 事故正是这样：日志只有两行、DB 状态卡死。
        try:
            _api.log(task_id, f"致命错误: {e}")
        except Exception:
            logger.debug("忽略异常（保留原行为）", exc_info=True)
        try:
            _api.task_store.finish(
                task_id,
                status="failed",
                success=success,
                registered=success + skipped + len(errors),
                skipped=skipped,
                errors=errors,
                error=str(e),
            )
        except Exception as exc:  # noqa: BLE001 - 收尾路径绝不能再抛
            logger.exception("批量任务失败收尾 finish 失败: %s: %s", task_id, exc)
        try:
            persist_task_snapshot(task_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("批量任务失败收尾快照失败: %s: %s", task_id, exc)
        _api.task_store.cleanup()
        return

    final_status = "stopped" if control.is_stop_requested() or stopped else "done"
    if final_status == "stopped":
        summary = (
            f"任务已停止: 成功 {success} 个, 跳过 {skipped} 个, 失败 {len(errors)} 个"
        )
    else:
        summary = f"完成: 成功 {success} 个, 跳过 {skipped} 个, 失败 {len(errors)} 个"
    # **收尾必须在 finally 里**：`_api.log()` 会走 `persist_task_snapshot` →
    # SQLite 写；并发写时 `database is locked` 会直接上抛。这条路径原来没有任何
    # 保护 —— 一旦抛出，`finish()` 永不执行，任务在内存里**永久 running**：
    # DELETE 409、stop 无人消费、`has_active()` 恒真连备份导入都拒绝
    # （actions 审计 F-03，实测）。
    try:
        _api.log(task_id, summary)
    except Exception as exc:  # noqa: BLE001 - 日志写不进去也要把任务结束掉
        logger.exception("任务收尾日志写入失败（继续结束任务）: %s: %s", task_id, exc)
    finally:
        try:
            _api.task_store.finish(
                task_id,
                status=final_status,
                success=success,
                registered=success + skipped + len(errors),
                skipped=skipped,
                errors=errors,
            )
        except Exception as exc:  # noqa: BLE001 - finish 自身失败只能记日志，不能二次抛出
            logger.exception("任务 finish 失败: %s: %s", task_id, exc)
        try:
            persist_task_snapshot(task_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("任务快照落库失败: %s: %s", task_id, exc)
        _api.task_store.cleanup()


def _load_account_fields(account_id: str, platform: str = "chatgpt") -> Optional[dict]:
    """把一行账号读成纯数据（含它注册时用的代理）。

    ``platform`` 必须由调用方传入：这里原来**两处**硬编码 ``"chatgpt"``
    （库名与 platform 过滤），于是 Grok 的批量动作虽然任务能建起来，
    执行时每个号都报「账号不存在」（实测 2026-10-28）。

    后面那几十秒网络请求期间不能占着数据库连接不放：连接池就那么几条，攥在手里
    会把面板其它请求一起拖住。代理字段也一并读出来 —— 复用账号时要优先回到
    它出生时的那个出口（见 `_run_account_batch_task` 里的代理选择）。
    """
    from services import task_service as _api  # 延迟 import：patch 打在 task_service 上必须被看到
    from core.db import AccountModel, platform_session

    with platform_session(platform) as s:
        account = s.get(AccountModel, account_id)
        if account is None or account.platform != platform:
            return None
        extra = account.get_extra()
        return {
            # 写回路径必须有主键：只按 email 写会跨平台改错号（真库 387 个邮箱
            # 同时存在于 chatgpt 与 grok，见 proxy 审计 P0-1/P0-2）。
            "account_id": str(account.id or ""),
            "platform": str(account.platform or platform or ""),
            "email": account.email,
            "password": account.password,
            "extra": extra,
            "token": account.token,
            "register_proxy": str(extra.get("register_proxy") or ""),
        }


def _run_account_batch_task(
    task_id: str,
    account_ids: list[int],
    *,
    label: str,
    concurrency: int = 1,
    delay_seconds: float = 0,
    proxy: Optional[str] = None,
    handle_account: Callable[..., AttemptResult],
    platform: str = "chatgpt",
) -> None:
    """「逐个号跑一遍」这类后台任务的调度骨架（补 RT、绑 2FA 都走这里）。

    ``platform`` 决定从哪个库读账号 —— Grok 的批量动作要靠它（原来硬编码
    chatgpt，任务建得起来但每个号都「不存在」）。

    排队限速、可停可跳、计数收尾这些每个批量任务都一样，只有每个号具体做什么
    不同 —— 那部分由 ``handle_account`` 提供，进度和日志复用注册任务那套。
    """
    from services import task_service as _api  # 延迟 import：patch 打在 task_service 上必须被看到
    from core.proxy_pool import proxy_pool
    from core.proxy_utils import normalize_proxy_url, redact_proxy_url

    control = _api.task_store.control_for(task_id)
    _api.task_store.mark_running(task_id)
    persist_task_snapshot(task_id)

    total = len(account_ids)
    success = 0
    skipped = 0
    errors: list[str] = []
    stopped = False
    start_gate = threading.Lock()
    next_start_time = time.time()

    def _resolve_proxy() -> Optional[str]:
        if proxy:
            return normalize_proxy_url(proxy)
        return normalize_proxy_url(proxy_pool.get_next())

    def _resolve_proxy_for_account(fields: dict) -> Optional[str]:
        """复用账号时挑代理：优先它注册时那个，不可用才换新的。

        **无绑定或绑定不可用时都会写回**账号的 `register_proxy` —— 用户要求
        「不再是仅有注册才绑定，若无绑定、绑定代理不可用，后续也能更新」。
        写回失败只记日志，不该让整轮任务失败。

        `fallback_provider` 传的是**函数**而不是 `_resolve_proxy()` 的结果：
        后者会立刻从池里取一个代理（推一格轮转游标），原代理可用时那格就白推了。
        """
        saved = str(fields.get("register_proxy") or "")
        chosen, should_bind = proxy_pool.resolve_for_account(
            saved, fallback_provider=_resolve_proxy
        )
        if should_bind and chosen:
            if saved:
                _api.log(
                    task_id,
                    f"原代理不可用，改用 {redact_proxy_url(chosen)}（已更新账号的代理字段）",
                )
            else:
                _api.log(
                    task_id,
                    f"账号未绑定代理，绑定 {redact_proxy_url(chosen)}（已写回账号的代理字段）",
                )
            _persist_register_proxy(
                fields.get("account_id", ""), fields.get("platform", ""), chosen
            )
        return chosen or None

    def _persist_register_proxy(account_id: str, platform: str, proxy_url: str) -> None:
        """把新代理写回账号的 `register_proxy`（只改这一个键）。

        原来这里硬编码 `platform="chatgpt"` + `platform == "chatgpt"`：

        - **Grok 任务**查不到行 → 直接 `return`，代理永不落库，下一轮又换出口
          （用户看到的「Grok 批量任务每次都用新代理」）；
        - 同邮箱的 **ChatGPT 号**被当成目标 → 改坏另一个平台（真库 387 个
          共享邮箱、286 个最新行是 grok，见 proxy 审计 P0-1）。

        写回走 `update_extra`：只 UPDATE `extra_json`，不拿整行快照覆盖，
        也不会在账号已删时把它复活（P0-3）。
        """
        if not account_id or not proxy_url:
            return
        from core.db import account_repository

        try:
            row = account_repository.get(str(account_id), str(platform or ""))
            if row is None:
                _api.log(task_id, "写回代理字段跳过：账号已不存在")
                return
            extra = row.get_extra()
            extra["register_proxy"] = proxy_url
            if not account_repository.update_extra(str(account_id), str(platform or ""), extra):
                _api.log(task_id, "写回代理字段跳过：账号已被删除")
        except Exception as exc:  # noqa: BLE001 - 记账失败不该让整轮任务失败
            _api.log(task_id, f"写回代理字段失败（忽略）: {exc}")

    def _wait_turn(attempt_id: int | None) -> None:
        nonlocal next_start_time
        if delay_seconds <= 0:
            return
        with start_gate:
            control.checkpoint(attempt_id=attempt_id)
            remaining = max(0.0, next_start_time - time.time())
            while remaining > 0:
                control.checkpoint(attempt_id=attempt_id)
                chunk = min(0.25, remaining)
                time.sleep(chunk)
                remaining -= chunk
            next_start_time = time.time() + delay_seconds

    def _do_one(index: int, account_id: str) -> AttemptResult:
        attempt_id: int | None = None
        try:
            control.checkpoint()
            attempt_id = control.start_attempt()
            _wait_turn(attempt_id)
            control.checkpoint(attempt_id=attempt_id)

            fields = _load_account_fields(account_id, platform)
            if fields is None:
                _api.log(task_id, f"[SKIP] 账号 #{account_id} 不存在")
                return AttemptResult.skipped("账号不存在")

            # 代理按账号定：优先它注册时那个，不可用才换（换完写回字段）。
            # 不能在这里先调 `_resolve_proxy()` —— 那会从池里取一个代理，
            # 把 `_index` 往前走一格，白消耗一个轮转位。
            account_proxy = _resolve_proxy_for_account(fields)

            _api.task_store.set_progress(task_id, f"{index + 1}/{total}")
            _api.log(task_id, f"开始{label} {index + 1}/{total}: {fields['email']}")
            if account_proxy:
                _api.log(task_id, f"使用代理: {_redact_proxy(account_proxy)}")

            result = handle_account(
                account_id=account_id,
                fields=fields,
                proxy=account_proxy,
                control=control,
                attempt_id=attempt_id,
            )
            if account_proxy:
                if result.outcome == AttemptOutcome.FAILED:
                    from core.proxy_utils import looks_like_proxy_error

                    if looks_like_proxy_error(result.message):
                        # 代理类失败：当场摘掉并给账号换一个绑定代理。
                        # `report_fail` 的阈值是「0 成功 + 5 次失败」，对
                        # 「跑到一半代理挂了」太慢 —— 用户要求当场切换。
                        proxy_pool.report_proxy_error(account_proxy)
                        old_proxy, new_proxy = proxy_pool.rebind(
                            fields.get("email", ""),
                            account_id=fields.get("account_id", ""),
                            platform=fields.get("platform", ""),
                        )
                        if new_proxy and new_proxy != old_proxy:
                            _api.log(
                                task_id,
                                f"代理异常，已切换绑定: {redact_proxy_url(old_proxy)}"
                                f" → {redact_proxy_url(new_proxy)}",
                            )
                    else:
                        proxy_pool.report_fail(account_proxy)
                elif result.outcome == AttemptOutcome.SUCCESS:
                    proxy_pool.report_success(account_proxy)
            return result
        except SkipCurrentAttemptRequested as e:
            _api.log(task_id, f"[SKIP] 已跳过当前账号: {e}")
            return AttemptResult.skipped(str(e))
        except StopTaskRequested as e:
            _api.log(task_id, f"[STOP] {e}")
            return AttemptResult.stopped(str(e))
        except Exception as e:
            _api.log(task_id, f"[FAIL] 账号 #{account_id} {label}异常: {e}")
            return AttemptResult.failed(str(e))
        finally:
            control.finish_attempt(attempt_id)

    try:
        from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed

        max_workers = max(1, min(int(concurrency or 1), max(total, 1)))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [
                pool.submit(_do_one, index, account_id)
                for index, account_id in enumerate(account_ids)
            ]
            for f in as_completed(futures):
                try:
                    result = f.result()
                except CancelledError:
                    continue
                except Exception as e:
                    _api.log(task_id, f"[ERROR] 任务线程异常: {e}")
                    errors.append(str(e))
                    continue
                if result.outcome == AttemptOutcome.SUCCESS:
                    success += 1
                elif result.outcome == AttemptOutcome.SKIPPED:
                    skipped += 1
                elif result.outcome == AttemptOutcome.STOPPED:
                    stopped = True
                else:
                    errors.append(result.message)
                _api.task_store.update_counters(
                    task_id,
                    success=success,
                    registered=success + skipped + len(errors),
                )
                persist_task_snapshot(task_id)
                if stopped or control.is_stop_requested():
                    stopped = True
                    for pending in futures:
                        if pending is not f:
                            pending.cancel()
    except Exception as e:
        # 收尾路径自己绝不能再抛：这里本来就在处理异常，若 _api._log 再抛一次，
        # finish() 会被整个跳过，任务永远停在 running —— 实测过的
        # `[Errno 32] Broken pipe` 事故正是这样：日志只有两行、DB 状态卡死。
        try:
            _api.log(task_id, f"致命错误: {e}")
        except Exception:
            logger.debug("忽略异常（保留原行为）", exc_info=True)
        _api.task_store.finish(
            task_id,
            status="failed",
            success=success,
            registered=success + skipped + len(errors),
            skipped=skipped,
            errors=errors,
            error=str(e),
        )
        persist_task_snapshot(task_id)
        _api.task_store.cleanup()
        return

    final_status = "stopped" if control.is_stop_requested() or stopped else "done"
    prefix = f"{label}已停止" if final_status == "stopped" else f"{label}完成"
    # 与 `_run_register` 同理：日志写失败不能把 finish() 一起带走（F-03）。
    try:
        _api.log(task_id, f"{prefix}: 成功 {success} 个, 跳过 {skipped} 个, 失败 {len(errors)} 个")
    except Exception as exc:  # noqa: BLE001
        logger.exception("批量任务收尾日志写入失败（继续结束任务）: %s: %s", task_id, exc)
    finally:
        try:
            _api.task_store.finish(
                task_id,
                status=final_status,
                success=success,
                registered=success + skipped + len(errors),
                skipped=skipped,
                errors=errors,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("批量任务 finish 失败: %s: %s", task_id, exc)
        try:
            persist_task_snapshot(task_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("批量任务快照落库失败: %s: %s", task_id, exc)
        _api.task_store.cleanup()


def _run_backfill_rt(task_id: str, account_ids: list[int], req: BackfillRtTaskRequest):
    """批量补 RT。逐号跑，可停可跳，进度和日志复用注册任务那套。"""
    from services import task_service as _api  # 延迟 import：patch 打在 task_service 上必须被看到
    from core.config_store import config_store
    from core.db import AccountModel
    from services.chatgpt_rt_backfill import apply_backfill_result, backfill_account_data

    base_config = config_store.get_all() or {}

    def _handle(*, account_id, fields, proxy, control, attempt_id) -> AttemptResult:
        email = fields["email"]
        result = backfill_account_data(
            email=email,
            password=fields["password"],
            extra=fields["extra"],
            token=fields["token"],
            config=base_config,
            proxy=proxy,
            allow_login=req.allow_login,
            log_fn=lambda msg: _api.log(task_id, f"  {msg}"),
            task_control=control,
            attempt_id=attempt_id,
        )

        from core.db import platform_session

        with platform_session("chatgpt") as s:
            account = s.get(AccountModel, account_id)
            if account is not None:
                apply_backfill_result(account, result, session=s, commit=True)

        if result.success:
            _api.log(task_id, f"[OK] {email} {result.summary()}")
            _api.save_task_log("chatgpt", email, "success", detail={"action": "backfill_rt"})
            return AttemptResult.success()

        _api.log(task_id, f"[FAIL] {email} {result.summary()}")
        _api.save_task_log(
            "chatgpt",
            email,
            "failed",
            error=result.summary(),
            detail={"action": "backfill_rt"},
        )
        return AttemptResult.failed(f"{email}: {result.summary()}")

    _run_account_batch_task(
        task_id,
        account_ids,
        label="补 RT",
        concurrency=req.concurrency,
        delay_seconds=req.delay_seconds,
        proxy=req.proxy,
        handle_account=_handle,
    )


def _run_probe_status(task_id: str, account_ids: list[int], req: "ProbeStatusTaskRequest"):
    """批量检测本地状态。逐号跑，可停可跳。

    走 `probe_local_status` 动作的**完整实现**（与单账号按钮同一条路径），
    结果落库复用 `_apply_action_result` —— 状态策略（封禁/过期/失效判定）
    只在那一处，两套路径不会漂移。

    与 `_run_refresh_token` 的差别：这个动作不发写请求给上游（只读探测），
    所以默认间隔可以很小（1 秒）；但探测本身要打一次 CLI Proxy，仍然串行
    更稳（并发可调）。
    """
    from services import task_service as _api  # 延迟 import：patch 打在 task_service 上必须被看到
    from core.config_store import config_store

    base_config = config_store.get_all() or {}

    def _handle(*, account_id, fields, proxy, control, attempt_id) -> AttemptResult:
        from services.action_execution import (
        apply_action_result as _apply_action_result,
        result_message as _result_message,
        to_platform_account as _to_platform_account,
    )
        from core.base_platform import RegisterConfig
        from core.db import AccountModel, platform_session
        from core.registry import get

        email = fields["email"]
        control.checkpoint(attempt_id=attempt_id)

        PlatformCls = get("chatgpt")
        instance = PlatformCls(config=RegisterConfig(extra=base_config))
        if proxy:
            instance.config.proxy = proxy
        instance._log_fn = lambda msg: _api.log(task_id, f"  {msg}")
        instance.bind_task_control(control)
        instance._task_attempt_token = attempt_id

        # 探测要打网络（几秒），不占着数据库连接 —— 与刷新/补 RT 同款。
        with platform_session("chatgpt") as s:
            row = s.get(AccountModel, account_id)
            if row is None:
                _api.log(task_id, f"[SKIP] 账号 #{account_id} 不存在")
                return AttemptResult.skipped("账号不存在")
            plat_account = _to_platform_account(row)

        result = instance.execute_action("probe_local_status", plat_account, {})

        with platform_session("chatgpt") as s:
            account = s.get(AccountModel, account_id)
            if account is not None:
                _apply_action_result("chatgpt", "probe_local_status", account, result, s)
                account_repository.save(account, session=s)
                s.commit()

        if result.get("ok"):
            message = _result_message(result) or "检测完成"
            _api.log(task_id, f"[OK] {email} {message}")
            _api.save_task_log(
                "chatgpt", email, "success", detail={"action": "probe_local_status"}
            )
            return AttemptResult.success()

        message = str(result.get("error") or _result_message(result) or "检测失败")
        _api.log(task_id, f"[FAIL] {email} {message}")
        _api.save_task_log(
            "chatgpt",
            email,
            "failed",
            error=message,
            detail={"action": "probe_local_status"},
        )
        return AttemptResult.failed(f"{email}: {message}")

    _run_account_batch_task(
        task_id,
        account_ids,
        label="检测本地状态",
        concurrency=req.concurrency,
        delay_seconds=req.delay_seconds,
        proxy=req.proxy,
        handle_account=_handle,
    )


def _run_batch_action(task_id: str, account_ids: list[int], req: "BatchActionTaskRequest"):
    """**通用**批量动作：逐号跑 `execute_action`，结果走共享落库路径。

    一个机制覆盖所有「批量点按钮」的场景（上传 / 状态同步 / Plus 检测 …），
    新增动作时不需要再写任务端点 —— 任务化是默认行为而不是逐个补齐。

    与 `_run_refresh_token` 的差别只在动作 id 是参数化的：那边是为刷新单独
    优化过（状态策略、token 列镜像），这里走通用 `_apply_action_result`。
    """
    from services import task_service as _api  # 延迟 import：patch 打在 task_service 上必须被看到
    from core.config_store import config_store

    base_config = config_store.get_all() or {}
    action_id = str(req.action_id or "")
    platform = str(req.platform or "").strip().lower()

    def _handle(*, account_id, fields, proxy, control, attempt_id) -> AttemptResult:
        from services.action_execution import (
        apply_action_result as _apply_action_result,
        result_message as _result_message,
        to_platform_account as _to_platform_account,
    )
        from core.base_platform import RegisterConfig
        from core.db import AccountModel, platform_session
        from core.registry import get

        email = fields["email"]
        control.checkpoint(attempt_id=attempt_id)

        PlatformCls = get(platform)
        instance = PlatformCls(config=RegisterConfig(extra=base_config))
        if proxy:
            instance.config.proxy = proxy
        instance._log_fn = lambda msg: _api.log(task_id, f"  {msg}")
        instance.bind_task_control(control)
        instance._task_attempt_token = attempt_id

        # 网络链不占数据库连接：先读成纯数据、归还连接，跑完再开短会话落库
        with platform_session(platform) as s:
            row = s.get(AccountModel, account_id)
            if row is None:
                _api.log(task_id, f"[SKIP] 账号 #{account_id} 不存在")
                return AttemptResult.skipped("账号不存在")
            # 禁用账号不参与面板上传（与 `_UPLOAD_ACTIONS` 的既有口径一致）
            from services.action_execution import (
            UPLOAD_ACTIONS as _UPLOAD_ACTIONS,
            is_banned_status as _is_banned_status,
        )

            if action_id in _UPLOAD_ACTIONS and _is_banned_status(row.status):
                _api.log(task_id, f"[SKIP] {email} 已禁用，不参与面板凭据上传")
                return AttemptResult.skipped("账号已禁用")
            plat_account = _to_platform_account(row)

        result = instance.execute_action(action_id, plat_account, dict(req.params or {}))

        with platform_session(platform) as s:
            account = s.get(AccountModel, account_id)
            if account is not None:
                _apply_action_result(platform, action_id, account, result, s)
                account_repository.save(account, session=s)
                s.commit()

        if result.get("ok"):
            message = _result_message(result) or "完成"
            _api.log(task_id, f"[OK] {email} {message}")
            _api.save_task_log(platform, email, "success", detail={"action": action_id})
            return AttemptResult.success()

        message = str(result.get("error") or _result_message(result) or "失败")
        _api.log(task_id, f"[FAIL] {email} {message}")
        _api.save_task_log(
            platform, email, "failed", error=message, detail={"action": action_id}
        )
        return AttemptResult.failed(f"{email}: {message}")

    _run_account_batch_task(
        task_id,
        account_ids,
        label=req.action_label or "批量操作",
        concurrency=req.concurrency,
        delay_seconds=req.delay_seconds,
        proxy=req.proxy,
        handle_account=_handle,
        platform=platform,
    )


def _run_refresh_token(task_id: str, account_ids: list[int], req: RefreshTokenTaskRequest):
    """批量刷新 Token。逐号跑，可停可跳，进度和日志复用注册任务那套。

    走 `refresh_token` 动作的完整链（session → OAuth → 登录兜底），
    与单账号按钮同一条路径 —— 结果落库也复用 `_apply_action_result`
    （状态策略、token 列镜像、凭证写回全在那一处）。
    """
    from services import task_service as _api  # 延迟 import：patch 打在 task_service 上必须被看到
    from core.config_store import config_store

    base_config = config_store.get_all() or {}

    def _handle(*, account_id, fields, proxy, control, attempt_id) -> AttemptResult:
        from services.action_execution import (
        apply_action_result as _apply_action_result,
        result_message as _result_message,
        to_platform_account as _to_platform_account,
    )
        from core.base_platform import RegisterConfig
        from core.db import AccountModel, platform_session
        from core.registry import get

        email = fields["email"]
        control.checkpoint(attempt_id=attempt_id)

        PlatformCls = get("chatgpt")
        instance = PlatformCls(config=RegisterConfig(extra=base_config))
        if proxy:
            instance.config.proxy = proxy
        instance._log_fn = lambda msg: _api.log(task_id, f"  {msg}")
        # 停止/跳过开关透进登录兜底（等码最长一个 OTP 超时，批量任务里要能
        # 当场打断）—— 与注册 runner 同款接线。
        instance.bind_task_control(control)
        instance._task_attempt_token = attempt_id

        # 网络链（几十秒）不能占着数据库连接：先把行读成纯数据、归还连接，
        # 跑完再开短会话落库 —— 与补 RT / 绑 2FA 同款（见 `_load_account_fields`
        # 的 docstring）。并发批量时每条连接都攥着不放会拖垮连接池。
        with platform_session("chatgpt") as s:
            row = s.get(AccountModel, account_id)
            if row is None:
                _api.log(task_id, f"[SKIP] 账号 #{account_id} 不存在")
                return AttemptResult.skipped("账号不存在")
            plat_account = _to_platform_account(row)

        result = instance.execute_action("refresh_token", plat_account, {})

        with platform_session("chatgpt") as s:
            account = s.get(AccountModel, account_id)
            if account is not None:
                _apply_action_result("chatgpt", "refresh_token", account, result, s)
                account_repository.save(account, session=s)
                s.commit()

        if result.get("ok"):
            message = _result_message(result) or "刷新完成"
            _api.log(task_id, f"[OK] {email} {message}")
            _api.save_task_log("chatgpt", email, "success", detail={"action": "refresh_token"})
            return AttemptResult.success()

        message = str(result.get("error") or _result_message(result) or "刷新失败")
        _api.log(task_id, f"[FAIL] {email} {message}")
        _api.save_task_log(
            "chatgpt",
            email,
            "failed",
            error=message,
            detail={"action": "refresh_token"},
        )
        return AttemptResult.failed(f"{email}: {message}")

    _run_account_batch_task(
        task_id,
        account_ids,
        label="刷新 Token",
        concurrency=req.concurrency,
        delay_seconds=req.delay_seconds,
        proxy=req.proxy,
        handle_account=_handle,
    )


def _run_recover_accounts(
    task_id: str, account_ids: list[str], req: "RecoverAccountsTaskRequest"
):
    """**异常账号尝试恢复**：逐个跑恢复链，救不回来的按退避计数、到上限落禁用。

    与 `_run_refresh_token` 的差别只有两处，但这两处正是用户要的语义：

    1. 恢复结果**写进退避计数器**（`record_auto_refresh_result`）—— 同一个号
       连续失败 3 次后 `disabled=True`，并且**当场落 `banned`**
       （`disabled_reason=recovery_exhausted`）。刷新任务不碰计数器，因为它
       是「用户点一次」的语义，不该累计到自动恢复的预算上。
    2. 成功时清掉计数器（`ok=True` → 清空），让号回到正常队列。
    """
    from services import task_service as _api
    from core.config_store import config_store

    base_config = config_store.get_all() or {}
    import time as _time

    def _handle(*, account_id, fields, proxy, control, attempt_id) -> AttemptResult:
        from services.action_execution import (
            apply_action_result as _apply_action_result,
            result_message as _result_message,
            to_platform_account as _to_platform_account,
        )
        from services.chatgpt_maintenance import record_auto_refresh_result
        from core.base_platform import RegisterConfig
        from core.db import AccountModel, account_repository, platform_session
        from core.registry import get

        email = fields["email"]
        control.checkpoint(attempt_id=attempt_id)

        PlatformCls = get("chatgpt")
        instance = PlatformCls(config=RegisterConfig(extra=base_config))
        if proxy:
            instance.config.proxy = proxy
        instance._log_fn = lambda msg: _api.log(task_id, f"  {msg}")
        instance.bind_task_control(control)
        instance._task_attempt_token = attempt_id

        with platform_session("chatgpt") as s:
            row = s.get(AccountModel, account_id)
            if row is None:
                _api.log(task_id, f"[SKIP] 账号 #{account_id} 不存在")
                return AttemptResult.skipped("账号不存在")
            before_status = str(row.status or "").strip().lower()
            plat_account = _to_platform_account(row)

        result = instance.execute_action("refresh_token", plat_account, {})

        recovered = bool(result.get("ok"))
        disabled_now = False
        attempts = 0
        with platform_session("chatgpt") as s:
            row = s.get(AccountModel, account_id)
            if row is not None:
                _apply_action_result("chatgpt", "refresh_token", row, result, s)
                # **退避计数 + 用尽落禁用**：这一步是「不成功则禁用」的落点。
                # `record_auto_refresh_result` 会在 attempts 达上限时把
                # `row.status` 改成 banned（并写 disabled_reason）。
                extra = row.get_extra()
                state = record_auto_refresh_result(
                    extra, ok=recovered, now=int(_time.time()), row=row
                )
                row.set_extra(extra)
                attempts = int((state or {}).get("attempts") or 0)
                disabled_now = str(row.status or "").strip().lower() == "banned" and before_status != "banned"
                account_repository.save(row, session=s)
                s.commit()

        if recovered:
            message = _result_message(result) or "恢复成功"
            _api.log(task_id, f"[OK] {email} {message}")
            _api.save_task_log(
                "chatgpt", email, "success", detail={"action": "recover"}
            )
            return AttemptResult.success()

        message = str(result.get("error") or _result_message(result) or "恢复失败")
        if disabled_now:
            _api.log(
                task_id,
                f"[DISABLED] {email} {message}"
                f" —— 连续恢复失败 {attempts} 次，已标注为禁用并移出恢复队列",
            )
            _api.save_task_log(
                "chatgpt",
                email,
                "failed",
                error=f"{message}（连续失败 {attempts} 次，已禁用）",
                detail={"action": "recover", "disabled": True},
            )
        else:
            _api.log(task_id, f"[FAIL] {email} {message}（第 {attempts} 次失败，将退避重试）")
            _api.save_task_log(
                "chatgpt",
                email,
                "failed",
                error=message,
                detail={"action": "recover", "attempts": attempts},
            )
        return AttemptResult.failed(f"{email}: {message}")

    _run_account_batch_task(
        task_id,
        account_ids,
        label="恢复账号",
        concurrency=req.concurrency,
        delay_seconds=req.delay_seconds,
        proxy=req.proxy,
        handle_account=_handle,
    )


def _run_bind_2fa(task_id: str, account_ids: list[int], req: Bind2faTaskRequest):
    """批量绑 2FA。和补 RT 同一套调度，区别只在每个号跑什么。"""
    from services import task_service as _api  # 延迟 import：patch 打在 task_service 上必须被看到
    from core.config_store import config_store
    from core.db import AccountModel
    from services.chatgpt_two_factor import (
        apply_two_factor_result,
        bind_account_two_factor,
        make_secret_persister,
    )

    base_config = config_store.get_all() or {}

    def _handle(*, account_id, fields, proxy, control, attempt_id) -> AttemptResult:
        email = fields["email"]
        result = bind_account_two_factor(
            email=email,
            password=fields["password"],
            extra=fields["extra"],
            token=fields["token"],
            config=base_config,
            proxy=proxy,
            allow_login=req.allow_login,
            log_fn=lambda msg: _api.log(task_id, f"  {msg}"),
            task_control=control,
            attempt_id=attempt_id,
            # 密钥一到手就落库：从「拿到」到下面那次 apply 之间还隔着 activate
            # 和复核两次网络往返，中间任务被停/进程退出都会丢密钥（丢了就锁死）
            persist_secret=make_secret_persister(
                account_id, log=lambda msg: _api.log(task_id, f"  {msg}")
            ),
        )

        from core.db import platform_session

        with platform_session("chatgpt") as s:
            account = s.get(AccountModel, account_id)
            if account is not None:
                apply_two_factor_result(account, result, session=s, commit=True)

        if result.ok:
            _api.log(task_id, f"[OK] {email} {result.summary()}")
            # 密钥只下发这一次，任务日志是用户当场导入验证器的唯一途径
            if result.secret:
                _api.log(task_id, f"  TOTP 密钥: {result.secret}")
            _api.save_task_log("chatgpt", email, "success", detail={"action": "bind_2fa"})
            return AttemptResult.success()

        if result.already_bound:
            _api.log(task_id, f"[SKIP] {email} {result.summary()}")
            return AttemptResult.skipped(f"{email}: {result.summary()}")

        _api.log(task_id, f"[FAIL] {email} {result.summary()}")
        _api.save_task_log(
            "chatgpt",
            email,
            "failed",
            error=result.summary(),
            detail={"action": "bind_2fa"},
        )
        return AttemptResult.failed(f"{email}: {result.summary()}")

    _run_account_batch_task(
        task_id,
        account_ids,
        label="绑 2FA",
        concurrency=req.concurrency,
        delay_seconds=req.delay_seconds,
        proxy=req.proxy,
        handle_account=_handle,
    )

