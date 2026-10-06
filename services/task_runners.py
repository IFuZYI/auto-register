"""    from api import tasks as _api  # 延迟 import：patch 打在 api.tasks 上必须被看到
注册 / 回填 RT / 绑定 2FA 的任务执行实现（原 api/tasks.py 的 470–1165 行）。

⚠️ 本模块的函数大量调用 `api/tasks.py` 里的辅助（`_api._log`/`_api._save_task_log`/
`_api._task_store`/`_api._persist_task_snapshot`/`_api._account_already_registered`/
`_api._auto_upload_integrations`/`_api.normalize_register_retry_times`/`_api.MAX_DEAD_END_ROUNDS`），
统一在**函数内** `from api import tasks as _api` 取门面 —— 这样
`patch("api.tasks._save_task_log")` 才能生效。这是全仓唯一需要函数内 import 的
地方：api.tasks → 本模块（模块级）与本模块 → api.tasks（函数级）构成单向循环。
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Callable, Optional

from sqlmodel import Session, select

from core.db import TaskLog, current_engine
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
        Bind2faTaskRequest,
        RefreshTokenTaskRequest,
        RegisterTaskRequest,
    )


def _run_register(task_id: str, req: RegisterTaskRequest):
    from api import tasks as _api  # 延迟 import：patch 打在 api.tasks 上必须被看到
    from core.registry import get
    from core.base_platform import RegisterConfig
    from core.db import save_account
    from modules.config import RegistrationContextBuilder
    from modules.mail import create_mailbox
    from core.proxy_utils import normalize_proxy_url

    control = _api._task_store.control_for(task_id)
    _api._task_store.mark_running(task_id)
    _api._persist_task_snapshot(task_id)
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
                from sqlmodel import select as _sel
                _active = _s.exec(
                    _sel(ProxyModel).where(ProxyModel.is_active == True)
                ).all()
                _prefetched_proxies = [p.url for p in _active if p.url]

        def _get_proxy() -> Optional[str]:
            if req.proxy:
                return req.proxy
            if _prefetched_proxies:
                with _prefetch_lock:
                    if _prefetched_proxies:
                        import random
                        return random.choice(_prefetched_proxies)
            return _proxy_pool.get_next()

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
            _api._log(
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
                    _api._log(
                        task_id,
                        f"[RETRY] 第 {i + 1} 个账号连续 {dead_end_rounds} 轮都栽在"
                        f"「重开也是同样结局」的失败上，剩下 "
                        f"{total_rounds - round_no + 1} 轮不再重开"
                        f"（再开只会多几个没人认领的号）: {result.message}",
                    )
                    break
                _api._log(
                    task_id,
                    f"[RETRY] 第 {i + 1} 个账号第 {round_no - 1}/{total_rounds} 轮失败，"
                    f"开始第 {round_no}/{total_rounds} 轮重试（全新会话）: {result.message}",
                )
                result = _do_one_round(i, round_no, total_rounds)
                dead_end_rounds = dead_end_rounds + 1 if _is_dead_end(result) else 0
            if result.outcome == AttemptOutcome.FAILED:
                # 注册记录按"一个序号一条结果"记，所以只有跑完所有轮次才落一条
                # failed；否则重试成功了还会在统计里留下一条失败
                _api._save_task_log(
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
                if req.register_delay_seconds > 0:
                    with start_gate_lock:
                        control.checkpoint(attempt_id=attempt_id)
                        now = time.time()
                        wait_seconds = max(0.0, next_start_time - now)
                        if wait_seconds > 0:
                            _api._log(
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
                _platform._log_fn = lambda msg: _api._log(task_id, msg)
                _platform.bind_task_control(control)
                if getattr(_platform, "mailbox", None) is not None:
                    _platform.mailbox._task_attempt_token = attempt_id
                    _platform.mailbox._log_fn = _platform._log_fn
                    # 告诉邮箱渠道「谁在用」—— 邮箱的消耗按平台记账：
                    # 同一个地址注册过 ChatGPT 之后还能注册 Grok。不注入的话
                    # 渠道退回旧的全局行为，会把地址对所有平台一起锁死。
                    _platform.mailbox._platform_name = req.platform
                _api._task_store.set_progress(task_id, f"{i + 1}/{req.count}")
                _api._persist_task_snapshot(task_id)
                _api._log(task_id, f"开始注册第 {i + 1}/{req.count} 个账号{round_suffix}")
                if _proxy:
                    _api._log(task_id, f"使用代理: {_proxy}")

                # 已注册的邮箱不重复注册：邮箱是账号唯一业务键，
                # 指定邮箱时先判重，命中就跳过（省掉整轮注册与验证码）。
                if req.email and _api._account_already_registered(req.platform, req.email):
                    _api._log(task_id, f"[SKIP] 该邮箱已注册，跳过: {req.email}")
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
                _api._log(task_id, f"[OK] 注册成功: {account.email}")
                _api._save_task_log(req.platform, account.email, "success")
                _api._auto_upload_integrations(task_id, saved_account or account)
                cashier_url = (account.extra or {}).get("cashier_url", "")
                if cashier_url:
                    _api._log(task_id, f"  [升级链接] {cashier_url}")
                    _api._task_store.add_cashier_url(task_id, cashier_url)
                    _api._persist_task_snapshot(task_id)
                return AttemptResult.success()
            except SkipCurrentAttemptRequested as e:
                _api._log(task_id, f"[SKIP] 已跳过当前账号: {e}")
                _api._save_task_log(
                    req.platform,
                    current_email,
                    "skipped",
                    error=str(e),
                )
                return AttemptResult.skipped(str(e))
            except StopTaskRequested as e:
                _api._log(task_id, f"[STOP] {e}")
                return AttemptResult.stopped(str(e))
            except Exception as e:
                if _proxy:
                    _proxy_pool.report_fail(_proxy)
                _api._log(task_id, f"[FAIL] 注册失败{round_suffix}: {e}")
                return AttemptResult.failed(
                    str(e),
                    retryable=not isinstance(e, NonRetryableRegisterError),
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
                    _api._log(task_id, f"[ERROR] 任务线程异常: {e}")
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
                _api._task_store.update_counters(
                    task_id,
                    success=success,
                    registered=success + skipped + len(errors),
                )
                _api._persist_task_snapshot(task_id)
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
            _api._log(task_id, f"致命错误: {e}")
        except Exception:
            pass
        _api._task_store.finish(
            task_id,
            status="failed",
            success=success,
            registered=success + skipped + len(errors),
            skipped=skipped,
            errors=errors,
            error=str(e),
        )
        _api._persist_task_snapshot(task_id)
        _api._task_store.cleanup()
        return

    final_status = "stopped" if control.is_stop_requested() or stopped else "done"
    if final_status == "stopped":
        summary = (
            f"任务已停止: 成功 {success} 个, 跳过 {skipped} 个, 失败 {len(errors)} 个"
        )
    else:
        summary = f"完成: 成功 {success} 个, 跳过 {skipped} 个, 失败 {len(errors)} 个"
    _api._log(task_id, summary)
    _api._task_store.finish(
        task_id,
        status=final_status,
        success=success,
        registered=success + skipped + len(errors),
        skipped=skipped,
        errors=errors,
    )
    _api._persist_task_snapshot(task_id)
    _api._task_store.cleanup()


def _load_account_fields(account_id: int) -> Optional[dict]:
    from api import tasks as _api  # 延迟 import：patch 打在 api.tasks 上必须被看到
    """把一行账号读成纯数据（含它注册时用的代理）。

    后面那几十秒网络请求期间不能占着数据库连接不放：连接池就那么几条，攥在手里
    会把面板其它请求一起拖住。代理字段也一并读出来 —— 复用账号时要优先回到
    它出生时的那个出口（见 `_run_account_batch_task` 里的代理选择）。
    """
    from core.db import AccountModel, platform_session

    with platform_session("chatgpt") as s:
        account = s.get(AccountModel, account_id)
        if account is None or account.platform != "chatgpt":
            return None
        extra = account.get_extra()
        return {
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
) -> None:
    """「逐个号跑一遍」这类后台任务的调度骨架（补 RT、绑 2FA 都走这里）。

    排队限速、可停可跳、计数收尾这些每个批量任务都一样，只有每个号具体做什么
    不同 —— 那部分由 ``handle_account`` 提供，进度和日志复用注册任务那套。
    """
    from api import tasks as _api  # 延迟 import：patch 打在 api.tasks 上必须被看到
    from core.proxy_pool import proxy_pool
    from core.proxy_utils import normalize_proxy_url, redact_proxy_url

    control = _api._task_store.control_for(task_id)
    _api._task_store.mark_running(task_id)
    _api._persist_task_snapshot(task_id)

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
                _api._log(
                    task_id,
                    f"原代理不可用，改用 {redact_proxy_url(chosen)}（已更新账号的代理字段）",
                )
            else:
                _api._log(
                    task_id,
                    f"账号未绑定代理，绑定 {redact_proxy_url(chosen)}（已写回账号的代理字段）",
                )
            _persist_register_proxy(fields.get("email", ""), chosen)
        return chosen or None

    def _persist_register_proxy(email: str, proxy_url: str) -> None:
        """把新代理写回账号的 `register_proxy`（就地改一个键，不覆盖其它）。"""
        if not email or not proxy_url:
            return
        from core.db import AccountModel, platform_session

        try:
            with platform_session("chatgpt") as s:
                row = s.exec(
                    select(AccountModel)
                    .where(AccountModel.platform == "chatgpt")
                    .where(AccountModel.email == email)
                ).first()
                if row is None:
                    return
                extra = row.get_extra()
                extra["register_proxy"] = proxy_url
                row.set_extra(extra)
                s.add(row)
                s.commit()
        except Exception as exc:  # noqa: BLE001 - 记账失败不该让整轮任务失败
            _api._log(task_id, f"写回代理字段失败（忽略）: {exc}")

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

    def _do_one(index: int, account_id: int) -> AttemptResult:
        attempt_id: int | None = None
        try:
            control.checkpoint()
            attempt_id = control.start_attempt()
            _wait_turn(attempt_id)
            control.checkpoint(attempt_id=attempt_id)

            fields = _load_account_fields(account_id)
            if fields is None:
                _api._log(task_id, f"[SKIP] 账号 #{account_id} 不存在")
                return AttemptResult.skipped("账号不存在")

            # 代理按账号定：优先它注册时那个，不可用才换（换完写回字段）。
            # 不能在这里先调 `_resolve_proxy()` —— 那会从池里取一个代理，
            # 把 `_index` 往前走一格，白消耗一个轮转位。
            account_proxy = _resolve_proxy_for_account(fields)

            _api._task_store.set_progress(task_id, f"{index + 1}/{total}")
            _api._log(task_id, f"开始{label} {index + 1}/{total}: {fields['email']}")
            if account_proxy:
                _api._log(task_id, f"使用代理: {account_proxy}")

            result = handle_account(
                account_id=account_id,
                fields=fields,
                proxy=account_proxy,
                control=control,
                attempt_id=attempt_id,
            )
            if account_proxy:
                if result.outcome == AttemptOutcome.FAILED:
                    proxy_pool.report_fail(account_proxy)
                elif result.outcome == AttemptOutcome.SUCCESS:
                    proxy_pool.report_success(account_proxy)
            return result
        except SkipCurrentAttemptRequested as e:
            _api._log(task_id, f"[SKIP] 已跳过当前账号: {e}")
            return AttemptResult.skipped(str(e))
        except StopTaskRequested as e:
            _api._log(task_id, f"[STOP] {e}")
            return AttemptResult.stopped(str(e))
        except Exception as e:
            _api._log(task_id, f"[FAIL] 账号 #{account_id} {label}异常: {e}")
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
                    _api._log(task_id, f"[ERROR] 任务线程异常: {e}")
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
                _api._task_store.update_counters(
                    task_id,
                    success=success,
                    registered=success + skipped + len(errors),
                )
                _api._persist_task_snapshot(task_id)
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
            _api._log(task_id, f"致命错误: {e}")
        except Exception:
            pass
        _api._task_store.finish(
            task_id,
            status="failed",
            success=success,
            registered=success + skipped + len(errors),
            skipped=skipped,
            errors=errors,
            error=str(e),
        )
        _api._persist_task_snapshot(task_id)
        _api._task_store.cleanup()
        return

    final_status = "stopped" if control.is_stop_requested() or stopped else "done"
    prefix = f"{label}已停止" if final_status == "stopped" else f"{label}完成"
    _api._log(task_id, f"{prefix}: 成功 {success} 个, 跳过 {skipped} 个, 失败 {len(errors)} 个")
    _api._task_store.finish(
        task_id,
        status=final_status,
        success=success,
        registered=success + skipped + len(errors),
        skipped=skipped,
        errors=errors,
    )
    _api._persist_task_snapshot(task_id)
    _api._task_store.cleanup()


def _run_backfill_rt(task_id: str, account_ids: list[int], req: BackfillRtTaskRequest):
    from api import tasks as _api  # 延迟 import：patch 打在 api.tasks 上必须被看到
    """批量补 RT。逐号跑，可停可跳，进度和日志复用注册任务那套。"""
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
            log_fn=lambda msg: _api._log(task_id, f"  {msg}"),
            task_control=control,
            attempt_id=attempt_id,
        )

        from core.db import platform_session

        with platform_session("chatgpt") as s:
            account = s.get(AccountModel, account_id)
            if account is not None:
                apply_backfill_result(account, result, session=s, commit=True)

        if result.success:
            _api._log(task_id, f"[OK] {email} {result.summary()}")
            _api._save_task_log("chatgpt", email, "success", detail={"action": "backfill_rt"})
            return AttemptResult.success()

        _api._log(task_id, f"[FAIL] {email} {result.summary()}")
        _api._save_task_log(
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


def _run_refresh_token(task_id: str, account_ids: list[int], req: RefreshTokenTaskRequest):
    from api import tasks as _api  # 延迟 import：patch 打在 api.tasks 上必须被看到
    """批量刷新 Token。逐号跑，可停可跳，进度和日志复用注册任务那套。

    走 `refresh_token` 动作的完整链（session → OAuth → 登录兜底），
    与单账号按钮同一条路径 —— 结果落库也复用 `_apply_action_result`
    （状态策略、token 列镜像、凭证写回全在那一处）。
    """
    from core.config_store import config_store

    base_config = config_store.get_all() or {}

    def _handle(*, account_id, fields, proxy, control, attempt_id) -> AttemptResult:
        from api.actions import _apply_action_result, _result_message, _to_platform_account
        from core.base_platform import RegisterConfig
        from core.db import AccountModel, platform_session
        from core.registry import get

        email = fields["email"]
        control.checkpoint(attempt_id=attempt_id)

        PlatformCls = get("chatgpt")
        instance = PlatformCls(config=RegisterConfig(extra=base_config))
        if proxy:
            instance.config.proxy = proxy
        instance._log_fn = lambda msg: _api._log(task_id, f"  {msg}")

        # 网络链（几十秒）不能占着数据库连接：先把行读成纯数据、归还连接，
        # 跑完再开短会话落库 —— 与补 RT / 绑 2FA 同款（见 `_load_account_fields`
        # 的 docstring）。并发批量时每条连接都攥着不放会拖垮连接池。
        with platform_session("chatgpt") as s:
            row = s.get(AccountModel, account_id)
            if row is None:
                _api._log(task_id, f"[SKIP] 账号 #{account_id} 不存在")
                return AttemptResult.skipped("账号不存在")
            plat_account = _to_platform_account(row)

        result = instance.execute_action("refresh_token", plat_account, {})

        with platform_session("chatgpt") as s:
            account = s.get(AccountModel, account_id)
            if account is not None:
                _apply_action_result("chatgpt", "refresh_token", account, result, s)
                s.add(account)
                s.commit()

        if result.get("ok"):
            message = _result_message(result) or "刷新完成"
            _api._log(task_id, f"[OK] {email} {message}")
            _api._save_task_log("chatgpt", email, "success", detail={"action": "refresh_token"})
            return AttemptResult.success()

        message = str(result.get("error") or _result_message(result) or "刷新失败")
        _api._log(task_id, f"[FAIL] {email} {message}")
        _api._save_task_log(
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


def _run_bind_2fa(task_id: str, account_ids: list[int], req: Bind2faTaskRequest):
    from api import tasks as _api  # 延迟 import：patch 打在 api.tasks 上必须被看到
    """批量绑 2FA。和补 RT 同一套调度，区别只在每个号跑什么。"""
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
            log_fn=lambda msg: _api._log(task_id, f"  {msg}"),
            task_control=control,
            attempt_id=attempt_id,
            # 密钥一到手就落库：从「拿到」到下面那次 apply 之间还隔着 activate
            # 和复核两次网络往返，中间任务被停/进程退出都会丢密钥（丢了就锁死）
            persist_secret=make_secret_persister(
                account_id, log=lambda msg: _api._log(task_id, f"  {msg}")
            ),
        )

        from core.db import platform_session

        with platform_session("chatgpt") as s:
            account = s.get(AccountModel, account_id)
            if account is not None:
                apply_two_factor_result(account, result, session=s, commit=True)

        if result.ok:
            _api._log(task_id, f"[OK] {email} {result.summary()}")
            # 密钥只下发这一次，任务日志是用户当场导入验证器的唯一途径
            if result.secret:
                _api._log(task_id, f"  TOTP 密钥: {result.secret}")
            _api._save_task_log("chatgpt", email, "success", detail={"action": "bind_2fa"})
            return AttemptResult.success()

        if result.already_bound:
            _api._log(task_id, f"[SKIP] {email} {result.summary()}")
            return AttemptResult.skipped(f"{email}: {result.summary()}")

        _api._log(task_id, f"[FAIL] {email} {result.summary()}")
        _api._save_task_log(
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

