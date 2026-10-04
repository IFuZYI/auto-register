"""Proxy pool backed by the application database."""

from datetime import datetime, timezone
import threading
from typing import Optional

from sqlmodel import Session, select

from .db import ProxyModel, current_engine
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

    def get_next(self, region: str = "") -> Optional[str]:
        """Return the next active proxy, biased toward higher success rate."""
        with Session(current_engine()) as s:
            q = select(ProxyModel).where(ProxyModel.is_active == True)
            if region:
                q = q.where(ProxyModel.region == region)
            proxies = s.exec(q).all()
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
                pass
            self.report_fail(p.url)
            results["fail"] += 1
        return results


proxy_pool = ProxyPool()
