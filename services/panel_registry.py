"""面板管理 —— 各平台面板的注册表（纯数据，无 IO）。

历史上这里还管着「本地插件」：把 CLIProxyAPI 源码 clone 到 `data/plugins/repos`、
用 `go build` 编译、拉起进程、写日志到 `data/plugins/logs`。那套逻辑已按用户要求
移除 —— 面板现在**全部是远程服务**，本应用只负责存它们的地址、提供跳转入口，
以及把注册好的账号推到它们那里去（见 `services/external_sync.py`）。

保留这个模块（而不是把常量塞进前端）的原因：GitHub 主页与地址字段的对应关系是
后端与前端共用的契约 —— `GET /api/integrations/panels` 把它发给界面，
`tests/test_panel_registry.py` 断言 key / url_key / secret_key 的形状，
避免前后端各写一份、改一边忘一边。

**新增一个面板时，除了这里，还要同步注册（少一处就会出现难查的症状）：**

1. `services/panel_comparison.py` 的 `FETCHERS` —— 漏了面板页报
   「未知面板: <key>」（404），实测踩过；
2. `services/panel_comparison_cache.py` 的 `_PANEL_PLATFORMS`（本地平台映射）
   与 `_panel_credentials`（凭据读取分支）—— 漏了对比拉不到数据；
3. 上传动作在对应平台插件里声明（`get_platform_actions`，`scope="panel"`），
   批量上传的结果落库在 `api/actions.py::_apply_action_result`（漏了界面永远
   看不到上传状态）；
4. 配置键：后端 `api/config.py` 的白名单 + 前端 `PanelConfigPanel.tsx` 的表单。

**多平台面板**（同时服务多个平台，如 CPA）额外要在注册表里声明 `platforms`
列表与 `upload_actions` / `sync_actions`（平台 → 动作 id）—— 对比页靠 `platforms`
决定是否渲染平台选择器（`shouldShowPlatformFilter`），靠后两个映射把批量动作
分发到账号各自的平台。只写兼容字段 `platform`（取第一个平台）的话，选择器
不会出现、批量动作也只会走第一个平台的接口。
"""

from __future__ import annotations

from typing import Any

#: 面板注册表。每项对应一个"注册完账号可以推过去"的外部系统。
#:
#: - `key`：前端与 API 里的稳定标识（公开契约，别改名）
#: - `label` / `desc`：界面文案
#: - `url_key`：这个面板的地址存在哪个配置键里（configs 表）
#: - `github`：项目主页。界面上的 GitHub 图标按钮直接开这个地址。
#:   值为空串表示"没有公开仓库"（自建/私有部署），前端不渲染图标按钮。
#: - `platform` / `upload_action` / `sync_action`：面板管理页上能直接跑的
#:   **平台动作**。账号页里那些「上传 CPA」「上传 Sub2API」「同步 CLIProxyAPI
#:   状态」原本只能在账号列表里操作；面板管理页既然已经在展示「谁没上传」，
#:   动作就该在同一个页面能发出去（用户要求「都集成到面板管理里面去」）。
#:   `sync_action` 为空表示这个面板没有"拉远端状态"这回事。
#:
#: **一个服务只占一项**：CLIProxyAPI 与「CPA 面板」是同一个服务（上传走
#: `/v0/management/auth-files`，状态同步读同一个端点），所以只有 `cpa` 一项。
#: 曾经并排摆两项、靠 `same_service_as` 声明共享配置键 —— 那只会让人以为是
#: 两个服务、各配一次。
PANELS: list[dict[str, Any]] = [
    {
        "key": "cpa",
        "label": "CPA 面板",
        "desc": "账号自动上传与状态同步目标（CLIProxyAPI 的 auth-files 接口）。ChatGPT 与 Grok 都走这里",
        "url_key": "cpa_api_url",
        # 本项目的 CPA 上传走 `/v0/management/auth-files`，那是 CLIProxyAPI 的
        # 管理端点，所以主页指向 CLIProxyAPI 本身而不是某个第三方面板。
        "github": "https://github.com/router-for-me/CLIProxyAPI",
        "secret_key": "cpa_api_key",
        "secret_label": "管理口令",
        "secret_placeholder": "默认 cliproxyapi",
        "url_placeholder": "http://127.0.0.1:8317",
        # 地址与口令统一在「全局配置 → 面板配置」填，本页只留卡片与跳转入口。
        # 这里不再渲染配置表单 —— 同一份配置在两个页面各渲染一次会让用户以为
        # 要分别填，改了一个另一个不生效。
        "config_form": False,
        # CLIProxyAPI 同时托管 ChatGPT（codex）与 Grok（xai）两类凭据，
        # 所以面板对应两个平台；上传/同步动作按账号所属平台各自分发。
        "platforms": ["chatgpt", "grok"],
        #: 动作 id 按平台查（同名的动作在两个平台上都有实现）
        "upload_actions": {"chatgpt": "upload_cpa", "grok": "upload_cpa"},
        "sync_actions": {"chatgpt": "sync_cliproxyapi_status", "grok": "sync_cliproxyapi_status"},
        #: CPA 里各平台对应的 auth-file provider（对比远端时按它过滤）
        "providers": {"chatgpt": "codex", "grok": "xai"},
        # 兼容字段：单平台消费方（老代码/脚本）仍读这两个键，取第一个平台
        "platform": "chatgpt",
        "upload_action": "upload_cpa",
        "sync_action": "sync_cliproxyapi_status",
    },
    {
        "key": "sub2api",
        "label": "Sub2API",
        "desc": "账号自动上传到 Sub2API 后台",
        "url_key": "sub2api_api_url",
        "github": "https://github.com/Wei-Shaw/sub2api",
        "secret_key": "sub2api_api_key",
        "platform": "chatgpt",
        "upload_action": "upload_sub2api",
        # Sub2API 没有"拉远端状态"这个动作（它只接收上传）。
        "sync_action": "",
    },
    {
        "key": "grok2api",
        "label": "grok2api",
        "desc": "Grok 账号池与 API 网关",
        "url_key": "grok2api_base_url",
        "github": "https://github.com/chenyme/grok2api",
        "platform": "grok",
        "upload_action": "upload_grok2api",
        "sync_action": "",
    },
    {
        "key": "chatgpt2api",
        "label": "chatgpt2api",
        "desc": "ChatGPT 普通网页号池（只认 access_token，与 CPA 的 codex 凭据互不相干）",
        "url_key": "chatgpt2api_api_url",
        # 同名项目有两个不同作者的实现，都兼容本项目（上传器同一份代码两边都能用）。
        # 主页指向 yukkcat 版：本项目的对比读取按它的字段形状对接（列表不带 token、
        # 凭证走 export），且线上部署实测就是这一版（v3.2.3）。
        "github": "https://github.com/yukkcat/chatgpt2api",
        "secret_key": "chatgpt2api_api_key",
        "secret_label": "管理密钥",
        "url_placeholder": "http://127.0.0.1:8000",
        # 只接收上传，没有"拉远端状态"这回事（与 Sub2API 同类）。
        "platform": "chatgpt",
        "upload_action": "upload_chatgpt2api",
        "sync_action": "",
    },
]

#: 面板 key → 注册表项，供后端按 key 查（例如测试脚本）
PANELS_BY_KEY: dict[str, dict[str, Any]] = {panel["key"]: panel for panel in PANELS}

#: 已删除的面板 key → 现存 key。
#:
#: `cliproxyapi` 曾经是独立一项，与 `cpa` 是同一个服务（同一端点、同一套配置键，
#: 见 `core/config_store._DUPLICATE_SERVICE_KEYS`），所以归一到 `cpa` 是**同一个
#: 服务**的归一，语义正确。
#:
#: 注意别把「已删除的**别的**服务」也塞进来：`team_manager` / `codex_proxy`
#: 已按用户要求整体删除，把它们归一到 `cpa` 会让调用方静默拿到另一个服务的
#: 配置 —— 那是错的，不如让 `resolve_panel_key` 原样返回、调用方自己发现它没了。
PANEL_KEY_ALIASES: dict[str, str] = {
    "cliproxyapi": "cpa",
}


def resolve_panel_key(key: str) -> str:
    """把（可能已废弃的）面板 key 归一成现存的 key；未知 key 原样返回。"""
    name = str(key or "").strip().lower()
    if name in PANELS_BY_KEY:
        return name
    return PANEL_KEY_ALIASES.get(name, name)


def list_panels() -> list[dict[str, Any]]:
    """返回面板清单（拷贝，调用方改不脏注册表）。"""
    return [dict(panel) for panel in PANELS]


__all__ = [
    "PANELS",
    "PANELS_BY_KEY",
    "PANEL_KEY_ALIASES",
    "resolve_panel_key",
    "list_panels",
]
