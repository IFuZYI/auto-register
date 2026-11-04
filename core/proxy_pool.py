"""Proxy pool backed by the application database."""

import logging

logger = logging.getLogger(__name__)
from datetime import datetime, timezone
import threading
from typing import Optional

from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from .db import ProxyModel, current_engine
from .models.proxy import TAG_BIND, TAG_REGISTER, parse_proxy_tags
from .proxy_utils import build_requests_proxy_config, normalize_proxy_url


class ProxyPool:
    def __init__(self):
        self._index = 0
        self._lock = threading.Lock()

    def _find_by_url(self, session: Session, url: str) -> ProxyModel | None:
        p = session.exec(select(ProxyModel).where(ProxyModel.url == url)).first()
        if p:
            return p

        normalized = normalize_proxy_url(url)
        if not normalized:
            return None

        if normalized != url:
            p = session.exec(
                select(ProxyModel).where(ProxyModel.url == normalized)
            ).first()
            if p:
                return p

        for candidate in session.exec(select(ProxyModel)).all():
            if normalize_proxy_url(candidate.url) == normalized:
                return candidate
        return None

    def get_next(self, region: str = "", tag: str = "") -> Optional[str]:
        """Return the next active proxy, biased toward higher success rate.

        `tag`：只在带该标签的代理里挑（注册链路传 `register`）。

        **带标签的一个都没有时回退到全部活跃代理** —— 存量部署升级上来时
        `tags` 全是空的，硬按标签过滤会让注册任务直接「没有可用代理」。
        回退是有意的：标签是「优先」，不是「唯一」。
        """
        with Session(current_engine()) as s:
            q = select(ProxyModel).where(ProxyModel.is_active == True)
            if region:
                q = q.where(ProxyModel.region == region)
            proxies = s.exec(q).all()
            if tag:
                tagged = [p for p in proxies if tag in parse_proxy_tags(getattr(p, "tags", ""))]
                if tagged:
                    proxies = tagged
                elif proxies:
                    logger.info(
                        "没有带 %r 标签的活跃代理，回退到全部活跃代理（%d 个）", tag, len(proxies)
                    )
            if not proxies:
                return None
            proxies.sort(
                key=lambda p: p.success_count / max(p.success_count + p.fail_count, 1),
                reverse=True,
            )
            with self._lock:
                idx = self._index % len(proxies)
                self._index += 1
            return proxies[idx].url

    def report_success(self, url: str) -> None:
        with Session(current_engine()) as s:
            p = self._find_by_url(s, url)
            if p:
                p.success_count += 1
                p.is_active = True
                p.last_checked = datetime.now(timezone.utc)
                s.add(p)
                s.commit()

    def report_fail(self, url: str) -> None:
        with Session(current_engine()) as s:
            p = self._find_by_url(s, url)
            if p:
                p.fail_count += 1
                p.last_checked = datetime.now(timezone.utc)
                if p.fail_count > 0 and p.success_count == 0 and p.fail_count >= 5:
                    p.is_active = False
                s.add(p)
                s.commit()

    # ── 标签与绑定池 ────────────────────────────────────────────────

    def list_bindable(self) -> list[ProxyModel]:
        """当前**可绑定**的代理（活跃 + 带 `bind` 标签）。"""
        with Session(current_engine()) as s:
            rows = s.exec(select(ProxyModel).where(ProxyModel.is_active == True)).all()
        return [
            row for row in rows if TAG_BIND in parse_proxy_tags(getattr(row, "tags", ""))
        ]

    def bound_counts(self) -> dict[str, dict]:
        """每个代理当前绑定了多少个账号（按平台拆分）。

        返回 `{url: {"total": n, "platforms": {平台: n}}}`。

        绑定关系存在账号的 `extra_json["register_proxy"]` 里 —— 没有单独的
        关联表，所以这里扫一遍账号。1000 个号实测 ~50ms，只在「打开代理页 /
        点均衡」时调用，不进热路径。
        """
        from .db import account_repository

        counts: dict[str, dict] = {}
        for account in account_repository.list_all_accounts():
            url = normalize_proxy_url(account.get_extra().get("register_proxy") or "")
            if not url:
                continue
            item = counts.setdefault(url, {"total": 0, "platforms": {}})
            item["total"] += 1
            platform = str(account.platform or "").strip().lower() or "unknown"
            item["platforms"][platform] = item["platforms"].get(platform, 0) + 1
        return counts

    @staticmethod
    def _balance_settings() -> tuple[bool, int]:
        """读「均衡配置」：`(是否按负载均衡, 单代理上限)`。

        配置读失败一律按默认（开 + 不限）—— 代理挑选不该因为读配置炸掉。
        """
        # import **放在 try 外面**：符号消失（`core.config_store` 里没有
        # `config_store`）必须立刻抛 ImportError，而不是被降级成默认值 ——
        # 否则「配置根本没接上」会伪装成「按默认走」，与仓库其它位置
        # 「数据源函数不存在却被宽 except 吞掉」是同一形状的静默故障。
        # 与 `services/task_runners.py` 的写法一致。
        from core.config_store import config_store

        try:
            raw_enabled = str(config_store.get("proxy_balance_enabled", "1") or "1")
            enabled = raw_enabled.strip().lower() not in {"0", "false", "off", "no"}
            raw_cap = str(config_store.get("proxy_balance_max_per_proxy", "0") or "0")
            cap = max(0, int(raw_cap.strip() or 0))
            return enabled, cap
        except (ValueError, TypeError, SQLAlchemyError):
            # 只有**真正的配置解析失败**才降级：读配置失败不该挡住绑定，
            # 按默认（开 + 不限）走。收窄到具体类型而不是 `except Exception`
            # —— 这个仓库有 `test_exception_handlers_not_too_wide` 守着，
            # 宽捕获会把栈吞掉。
            return True, 0

    def pick_bindable(self, *, exclude: str = "") -> Optional[str]:
        """从可绑定池里挑一个（默认**负载最轻**的，并列时随机）。

        用户要求「均衡」：绑定不能全堆在第一个代理上。按当前绑定数升序，
        同负载的用随机打散，避免每次都挑中同一个。

        `proxy_balance_enabled=0` → 纯随机（老行为）。
        `proxy_balance_max_per_proxy=N` → 只考虑当前绑定数 < N 的代理；
        全都到顶时忽略上限（否则新账号无处可绑，比超一点更糟）。
        """
        import random

        candidates = self.list_bindable()
        if not candidates:
            return None
        counts = self.bound_counts()
        excluded = normalize_proxy_url(exclude) if exclude else ""
        urls = [
            row.url
            for row in candidates
            if normalize_proxy_url(row.url) != excluded
        ]
        if not urls:
            return None

        enabled, cap = self._balance_settings()
        if not enabled:
            return random.choice(urls)

        scored = [
            (counts.get(normalize_proxy_url(url), {}).get("total", 0), url) for url in urls
        ]
        if cap > 0:
            under_cap = [(score, url) for score, url in scored if score < cap]
            if under_cap:
                scored = under_cap
        lightest = min(score for score, _ in scored)
        pool = [url for score, url in scored if score == lightest]
        return random.choice(pool)

    def rebind(
        self,
        account_email: str = "",
        *,
        account_id: str = "",
        platform: str = "",
    ) -> tuple[str, str]:
        """给一个账号重新挑绑定代理并写回 `register_proxy`。

        返回 `(旧值, 新值)`；新值为空表示没有可绑定的代理（保持原样）。

        **必须按 id（或 email + platform）定位**。原来的实现只按 email 在所有
        平台库里找、取 `created_at` 最新的一行 —— 实测真库 670 个邮箱里 387 个
        同时存在于 chatgpt 与 grok，其中 286 个最新行是 grok。于是「ChatGPT
        任务里代理挂了」会把 **Grok 号**的绑定改掉（全量检测 proxy P0-2）。

        写回也**不走 `upsert`**：那是整行快照覆盖，会把并发写入的
        token/password/状态回滚，还会把已删除的账号以新 UUID 复活（P0-3）。
        """
        from .db import account_repository

        account = None
        if account_id:
            account = account_repository.get(str(account_id), str(platform or ""))
        elif account_email and platform:
            account = account_repository.find_by_email(str(platform), str(account_email))
        elif account_email:
            # 没给 platform：先看这个邮箱到底存在于几个平台
            # `platforms_by_email` 返回 {email: {platform, ...}}
            mapping = account_repository.platforms_by_email([str(account_email)]) or {}
            names = sorted({name for names in mapping.values() for name in names})
            candidates = []
            for name in names:
                # 注意参数顺序：`find_by_email(platform, email)`（平台在前）
                row = account_repository.find_by_email(name, str(account_email))
                if row is not None:
                    candidates.append(row)
            if len(candidates) > 1:
                # 跨平台同名邮箱：没有 platform 就**不要猜**，猜错=改坏另一个平台
                logger.warning(
                    "rebind 拒绝按 email 跨平台定位（%s 命中 %d 个平台），"
                    "调用方必须传 platform 或 account_id",
                    account_email,
                    len(candidates),
                )
                return "", ""
            account = candidates[0] if candidates else None
        if account is None:
            return "", ""

        old = normalize_proxy_url(account.get_extra().get("register_proxy") or "")
        # 归一化后再落库/返回：`pick_bindable` 给的是库里的原始串（可能是
        # `socks5://`），而 `old` 已经过 `normalize_proxy_url`（→ `socks5h://`）。
        # 不统一的话调用方拿到的 (old, new) 两种写法，比较必然不等，
        # 日志里也会出现「切换到了同一个代理」的假象。
        chosen = normalize_proxy_url(self.pick_bindable(exclude=old) or "")
        if not chosen:
            return old, old
        extra = dict(account.get_extra())
        extra["register_proxy"] = chosen
        saved = account_repository.update_extra(
            getattr(account, "id", ""), str(getattr(account, "platform", "") or ""), extra
        )
        if not saved:
            # 账号在挑选期间被删了：不要 upsert 把它复活
            logger.warning(
                "rebind 写回失败（账号已不存在）: id=%s platform=%s",
                getattr(account, "id", ""),
                getattr(account, "platform", ""),
            )
            return old, old
        return old, chosen

    def rebalance(self, *, dry_run: bool = True) -> dict:
        """把「绑定不合规」的账号均衡地挪到可绑定池。

        不合规的三种（用户点名的）：**没绑定** / 绑定的代理**没有 `bind` 标签**
        / 绑定的代理**已被删除**。合规的不动 —— 均衡不该把已经在池里的号
        反复搬家。

        目标是「最轻的那个」：每次分配都重新读一次计数，所以是逐号贪心，
        最终每个代理的账号数相差不超过 1。
        """
        from .db import account_repository

        bindable = {
            normalize_proxy_url(row.url)
            for row in self.list_bindable()
        }
        if not bindable:
            return {"changed": [], "skipped": 0, "reason": "没有带 bind 标签的可用代理"}

        counts = self.bound_counts()
        accounts = account_repository.list_all_accounts()
        changed: list[dict] = []
        skipped = 0
        for account in accounts:
            extra = dict(account.get_extra())
            current = normalize_proxy_url(extra.get("register_proxy") or "")
            if current and current in bindable:
                skipped += 1
                continue
            # 挑当前负载最轻的（并列取第一个，保证 dry-run 与实际一致）
            target = min(
                bindable,
                key=lambda url: (counts.get(url, {}).get("total", 0), url),
            )
            changed.append(
                {
                    "email": str(account.email or ""),
                    "platform": str(account.platform or ""),
                    "from": current,
                    "to": target,
                }
            )
            counts.setdefault(target, {"total": 0, "platforms": {}})
            counts[target]["total"] += 1
            if not dry_run:
                extra["register_proxy"] = target
                saved = account_repository.update_extra(
                    getattr(account, "id", ""),
                    str(getattr(account, "platform", "") or ""),
                    extra,
                )
                if not saved:
                    # 期间被删：不 upsert（会以新 UUID 复活的账号比没均衡更糟）
                    changed.pop()
                    skipped += 1
        return {"changed": changed, "skipped": skipped}

    def report_proxy_error(self, url: str) -> None:
        """代理**连接类**错误：记一次失败并立刻摘掉。

        `report_fail` 的阈值是「0 成功 + 5 次失败」，对「任务跑到一半代理挂了」
        太慢 —— 用户要求当场切换。这里直接置 `is_active=False`，下一次
        `resolve_for_account` 就会把这个号换到别的可绑定代理上。
        """
        normalized = normalize_proxy_url(url)
        if not normalized:
            return
        with Session(current_engine()) as s:
            row = self._find_by_url(s, normalized)
            if row is None:
                return
            row.fail_count += 1
            row.last_checked = datetime.now(timezone.utc)
            row.is_active = False
            s.add(row)
            s.commit()

    def is_available(self, url: str) -> bool:
        """这个代理当前可用吗（在池里且没被停用）。

        「不可用」的判定依据是池里已有的健康信号：`report_fail` 在「0 成功 +
        5 次失败」时会置 `is_active=False`，界面上的「检测全部代理」也会逐条
        真实探活并据此改状态。**不在池里的代理视为可用** —— 那多半是调用方
        手工指定的一次性代理（`req.proxy`），我们没有任何证据说它坏了，
        不该替用户把它换掉。
        """
        normalized = normalize_proxy_url(url)
        if not normalized:
            return False
        with Session(current_engine()) as s:
            row = self._find_by_url(s, normalized)
        if row is None:
            return True
        return bool(row.is_active)

    def resolve_for_account(
        self, saved_proxy: str, *, fallback: str = "", fallback_provider=None
    ) -> tuple[str, bool]:
        """给「复用已有账号」的场景挑代理：优先用账号**注册时那个**。

        同一个账号反复走不同的出口 IP 容易被上游判定为异常登录，所以复用时
        应该尽量回到它出生时的那个代理。

        返回 `(要用的代理, 是否需要更新绑定)`。第二个值为真时调用方应当把
        `chosen` 写回账号的 `register_proxy` 字段。判据是**选中的值和账号当前
        记录的值不同**，两种情况都算：

        - **原来没绑定** → 把这次实际用的代理绑上（`""` → 池里取的那个）。
          这正是用户要的「不再是仅有注册才绑定」：注册时没走代理（或代理池
          当时是空的）的账号，之后第一次复用就补上绑定，此后固定走它。
          不补的话每次复用都从池里轮换取一个，出口 IP 一直在变。
        - **原来绑的那个已不可用** → 换成新的（用户要的「绑定代理不可用时
          后续也能更新」）。

        原值可用或无法替换时返回 `False`，调用方不写库。

        `fallback_provider` 是**惰性**的备用代理来源（一个无参可调用对象）。
        用它而不是直接传 `fallback` 字符串，是因为取备用代理（`get_next()`）
        有副作用：它会把池子的轮转游标往前推一格。原代理还能用的时候白推一格，
        会让真正需要备用的那些账号拿到偏离预期的代理。

        几条边界：
          - 原代理在池里且可用 → 原样返回，不更新
          - 原代理在池里但被停用 → 换备用并更新；**备用为空时保留原值**
            （总比清空好 —— 代理可能只是被误停用，下次探活会复活它）
          - 原代理不在池里 → 视为可用（手工指定的一次性代理，见 `is_available`）
          - 备用为空且原来也没绑定 → 直连，不写库（没什么可绑的）
        """
        saved = normalize_proxy_url(saved_proxy) or ""

        # ── 绑定规则（用户 2026-10-30）──
        # 「绑定的代理有 `bind` 标签」→ 就用它；否则（没绑 / 没标签 / 已删除）
        # 从可绑定池里挑一个**负载最轻**的绑上。
        # 可绑定池为空时回退到旧行为（原值可用就保留，否则取备用），
        # 这样没配标签的存量部署行为不变。
        bindable = self.list_bindable()
        if bindable:
            bindable_urls = {normalize_proxy_url(row.url) for row in bindable}
            if saved and saved in bindable_urls and self.is_available(saved):
                return saved, False
            replacement = self.pick_bindable(exclude=saved)
            if replacement:
                return replacement, replacement != saved

        if not saved:
            chosen = self._pick_fallback(fallback, fallback_provider)
            # 原来没绑定：把这次用的绑上（为空则无事可做）
            return chosen, bool(chosen)

        if self.is_available(saved):
            return saved, False

        replacement = self._pick_fallback(fallback, fallback_provider)
        if not replacement or replacement == saved:
            # 没有更好的选择：保留原值，不要把字段清空
            return saved, False
        return replacement, True

    @staticmethod
    def _pick_fallback(fallback: str, fallback_provider) -> str:
        """解析备用代理：给了惰性来源就用它，否则用传进来的字符串。"""
        if callable(fallback_provider):
            try:
                return normalize_proxy_url(fallback_provider()) or ""
            except Exception:
                return normalize_proxy_url(fallback) or ""
        return normalize_proxy_url(fallback) or ""

    def reactivate_all(self) -> int:
        """把所有被自动停用的代理重新启用，返回启用条数。

        为什么需要：`report_fail` 在「0 成功 + 5 次失败」时会 `is_active=False`，
        而 `get_next()` 只取 active 的 —— 于是一个**本来能用**的代理只要连撞 5 次
        失败（例如那 5 次用的是不支持它的调用路径），就会被永久拉黑，之后无论
        怎么重试都取不到它，界面还会显示成「没有可用代理」。实测踩到过：
        住宅代理是活的（curl 直接连通），却因为 6 次失败躺在库里 is_active=0。

        界面上「检测全部代理」会逐条真实探活并 `report_success` 自动复活；
        这个方法是给「我知道代理没问题、想直接恢复」的场景用的。
        """
        count = 0
        with Session(current_engine()) as s:
            for p in s.exec(select(ProxyModel)).all():
                if not p.is_active:
                    p.is_active = True
                    p.fail_count = 0
                    p.last_checked = datetime.now(timezone.utc)
                    s.add(p)
                    count += 1
            s.commit()
        return count

    def check_all(self) -> dict:
        """Probe all configured proxies against a neutral endpoint."""
        import requests

        with Session(current_engine()) as s:
            proxies = s.exec(select(ProxyModel)).all()
        results = {"ok": 0, "fail": 0}
        for p in proxies:
            try:
                r = requests.get(
                    "https://httpbin.org/ip",
                    proxies=build_requests_proxy_config(p.url),
                    timeout=8,
                )
                if r.status_code == 200:
                    self.report_success(p.url)
                    results["ok"] += 1
                    continue
            except Exception:
                logger.debug("忽略异常（保留原行为）", exc_info=True)
            self.report_fail(p.url)
            results["fail"] += 1
        return results


proxy_pool = ProxyPool()
