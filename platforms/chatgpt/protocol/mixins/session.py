"""SessionMixin：TLS 错误判定、指纹轮换与会话同步。

从 3541 行的 auth_flow.py 拆出（纯搬家，方法体逐字节不变）。

⚠️ 本模块**必须自己 import `create_http_session`**：`_rotate_impersonate_session`
会用它建会话，测试用 `mock.patch.object(auth_flow_module, "create_http_session")`
打桩 —— 但 `warmup`/`check_proxy` 永久留在 auth_flow.py，所以两处各持一个模块级
`create_http_session` 名字，各自的 patch 互不影响（这正是想要的）。
"""
from __future__ import annotations

import logging

from platforms.chatgpt.protocol.fingerprint import (
    family_impersonates,
    fingerprint_for_impersonate,
    ua_for_impersonate,
)
from platforms.chatgpt.protocol.http_client import create_http_session

logger = logging.getLogger(__name__)


class SessionMixin:
    @staticmethod
    def _is_tls_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        markers = ["curl: (35)", "tls connect error", "openssl_internal", "sslerror"]
        return any(m in msg for m in markers)


    def _rotate_impersonate_session(self) -> bool:
        """仅在 curl_cffi 指纹模式内切换 UA 指纹版本重试，同时联动更新 UA。

        ⚠️ 这里必须连 self._fingerprint 里的 client hints 一起换掉。
        旧版只更新了 self._ua 和 session —— 但 _common_headers / _navigation_headers
        的 sec-ch-ua* 全是从 self._fingerprint 取的，于是换完会变成
        「UA 说 Chrome/136、sec-ch-ua 说 v=146」，连 not_a_brand 都对不上
        （三个版本各不相同："Not.A/Brand";v="99" / "Not/A)Brand";v="8" /
        "Not?A_Brand";v="99"）—— 这正是上一轮刚消灭的「UA 与头自相矛盾」，
        是 CF 最容易抓的特征。之前没爆只因这条路几乎没走到过。

        fallback_impersonates 是**同家族**构造的（见 fingerprint.py 各 _gen_*），
        所以只会 chrome→chrome、safari→safari，不会跨族；但同族换版本一样要同步头。
        """
        if self._impersonate_idx >= len(self._impersonate_candidates) - 1:
            return False
        self._impersonate_idx += 1
        imp = self._impersonate_candidates[self._impersonate_idx]
        self._sync_fingerprint_to(imp)
        logger.warning(f"TLS 异常，切换指纹重试: impersonate={imp}, ua={self._ua[:60]}...")
        self.session = create_http_session(
            proxy=self.config.proxy, impersonate=imp, user_agent=self._ua,
        )
        return True


    def _sync_fingerprint_to(self, impersonate: str) -> None:
        """换 impersonate 时把 UA 和 client hints 一起对齐。

        少了任何一样都会造出「UA 说 Chrome、sec-ch-ua 说别的」这种自相矛盾的头，
        那是 CF 一抓一个准的特征。

        UA 必须**从新指纹里取**，不能自己再调一次 ua_for_impersonate：那个函数每
        次都会重新随机一个系统版本，调两次就会得到「会话 UA 说 macOS 14_4、指纹
        里记的是 14_5」这种同样自相矛盾的组合。
        """
        try:
            self._fingerprint = fingerprint_for_impersonate(impersonate, self._fingerprint)
            self._ua = self._fingerprint.get("user_agent") or self._ua
        except Exception as e:  # 兜底：宁可维持旧指纹也不要把流程搞崩
            logger.warning(f"client hints 同步失败（沿用旧指纹）: {e}")
            self._ua = ua_for_impersonate(impersonate, self._ua)


    def _switch_browser_family(self, impersonate: str) -> None:
        """跨家族换指纹：UA、client hints、同族回退列表一起换掉。

        同族回退列表也得跟着走，否则之后遇到 TLS 异常会跳回原来那个家族。
        """
        self._sync_fingerprint_to(impersonate)
        self._impersonate_candidates = family_impersonates(impersonate)
        self._impersonate_idx = 0


    def _sentinel_fp_kwargs(self) -> dict:
        """从 self._fingerprint 抽出 sentinel 需要的指纹/硬件字段。

        保证 4 处 sentinel 调用（authorize_continue / username_password_create /
        create_account 等）用的是同一套一致画像——UA↔platform↔vendor↔硬件全程不变。
        """
        fp = self._fingerprint or {}
        return {
            "user_agent": self._ua,
            "sec_ch_ua": fp.get("sec_ch_ua", ""),
            "sec_ch_ua_platform": fp.get("sec_ch_ua_platform", ""),
            "sec_ch_ua_mobile": fp.get("sec_ch_ua_mobile", ""),
            # Client Hints 全套（仅 Chromium 有值）
            "sec_ch_ua_full_version_list": fp.get("sec_ch_ua_full_version_list", ""),
            "sec_ch_ua_arch": fp.get("sec_ch_ua_arch", ""),
            "sec_ch_ua_bitness": fp.get("sec_ch_ua_bitness", ""),
            "sec_ch_ua_model": fp.get("sec_ch_ua_model", ""),
            "sec_ch_ua_platform_version": fp.get("sec_ch_ua_platform_version", ""),
            "screen": fp.get("screen", ""),
            "lang": fp.get("lang", ""),
            "lang_full": fp.get("lang_full", ""),
            "browser_type": fp.get("browser_type", ""),
            "navigator_platform": fp.get("navigator_platform", ""),
            "navigator_vendor": fp.get("navigator_vendor"),
            "hardware_concurrency": fp.get("hardware_concurrency", 0),
            "device_memory": fp.get("device_memory"),
            "max_touch_points": fp.get("max_touch_points", 0),
            "device_pixel_ratio": fp.get("device_pixel_ratio", 0.0),
            "timezone": fp.get("timezone", ""),  # IP 联动时区
        }
