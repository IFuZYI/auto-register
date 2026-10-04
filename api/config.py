import json
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from core.config_store import config_store
from services.mail_imports import (
    align_source_with_provider,
    normalize_mail_import_source,
    normalize_mail_provider,
)
from services.sms_service import SMS_DEFAULT_COUNTRY, SMS_DEFAULT_SERVICE

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/config", tags=["config"])

#: 口令类配置键：`GET /api/config` **不回明文**，只回 `""` 加一个 `<key>_set` 布尔。
#:
#: 为什么要在服务端做：以前前端只是把口令清空后再回填表单，但接口本身仍在明文
#: 下发 —— 打开开发者工具的网络面板、或直接 curl 就能读到全部口令。真正的
#: 「输入后不可查看」必须让明文根本不离开服务端。
#:
#: 写入侧配套：提交空串表示「不修改」（见 `update_config`），否则页面一保存就会
#: 把已存的口令抹成空。要清空口令请显式提交 `null`。
SECRET_CONFIG_KEYS: frozenset[str] = frozenset({
    "yescaptcha_key",
    "twocaptcha_key",
    "cpa_api_key",
    "chatgpt2api_api_key",
    "cliproxyapi_management_key",  # `cpa_api_key` 的别名键（老数据兜底）
    "sub2api_api_key",
    "grok2api_password",
    "sms_api_key",
    "contribution_key",
    "custom_contribution_token",
})

#: `GET /api/config` 额外回给前端的「是否已设置」标记：`<key>_set`。
#: 界面靠它显示「已配置 / 未配置」，不必看到明文。
SECRET_SET_SUFFIX = "_set"


def _secret_set_flags(all_cfg: dict) -> dict:
    """口令键 → 是否已设置（非空）。前端据此提示「留空 = 不修改」。"""
    return {
        f"{key}{SECRET_SET_SUFFIX}": bool(str(all_cfg.get(key, "") or "").strip())
        for key in sorted(SECRET_CONFIG_KEYS)
    }


CONFIG_KEYS = [
    "yescaptcha_key",
    "twocaptcha_key",
    "default_executor",
    "default_captcha_solver",
    "register_retry_times",
    # 注册任务的默认参数（任务页打开时预填这些值，仍可临时改）
    "register_count",
    "register_concurrency",
    "register_delay_seconds",
    # 各平台的执行器（按平台单独设置，没有全局默认 —— 各平台支持的集合不同）
    "chatgpt_executor",
    "grok_executor",
    "mail_provider",
    "mail_import_source",
    "outlook_backend",
    "mailbox_otp_timeout_seconds",
    # 本地 iCloud 渠道（主号在 data/platforms/icloud.db，别名由本项目生成）
    "icloud_local_account_id",
    "icloud_local_label",
    "icloud_local_note",
    "cpa_enabled",
    "cpa_api_url",
    "cpa_api_key",
    # CPA 自动上传按平台分开开关。`cpa_enabled` 是历史单开关，作为两者都未设置时
    # 的兜底（老配置 cpa_enabled=true 的语义是「都传」，见 services/external_sync.py）。
    "cpa_upload_chatgpt_enabled",
    "cpa_upload_grok_enabled",
    # 上传凭据时是否带上账号绑定的代理（`register_proxy`）。CPA 的 auth 文件
    # 顶层支持 `proxy_url`、chatgpt2api 的导入对象支持 `proxy` 字段 —— 开关
    # 按面板分开（导入格式各自独立，合成一个会连带另一边）。默认关。
    "cpa_upload_proxy_enabled",
    "chatgpt2api_upload_proxy_enabled",
    "sub2api_enabled",
    "sub2api_api_url",
    "sub2api_api_key",
    "sub2api_group_ids",
    "chatgpt2api_enabled",
    "chatgpt2api_api_url",
    "chatgpt2api_api_key",
    "cliproxyapi_base_url",
    "cliproxyapi_management_key",
    "grok2api_base_url",
    "grok2api_username",
    "grok2api_password",
    # grok2api 的自动上传开关（注册完自动把账号推过去）。与 `cpa_enabled` /
    # `sub2api_enabled` 同一个语义：留空按「已配置就开」处理。
    "grok2api_enabled",
    # 兼容键：注册方式已并入「执行器」（`grok_executor`）。协议注册路径删除后
    # 这个键对行为**没有影响**（只有一条浏览器路径），但老任务/脚本仍会提交它 ——
    # 保留在白名单里，免得那些调用整批被静默忽略（响应会回 ignored 列表）。
    "grok_register_mode",
    # `grok_send_code_mode` / `grok_send_code_timeout` 已随协议路径删除：
    # 它们只被那条路读取，留着会让用户以为能调（实际无人读）。
    "grok_send_code_min_interval",
    # ChatGPT 的注册方式全局默认值（原先只在任务页 localStorage 里选，
    # 换机器就丢）。见 platforms/chatgpt/plugin.py 的 registration_modes。
    "chatgpt_registration_mode",
    "chatgpt_register_flow",
    "sms_enabled",
    "sms_provider",
    "sms_api_key",
    "sms_service",
    "sms_country",
    "sms_auto_country",
    "sms_allowed_countries",
    "sms_auto_min_stock",
    "sms_auto_max_price",
    "sms_max_price",
    "sms_fixed_price",
    "sms_reuse_phone",
    "sms_phone_success_max",
    "sms_per_phone_timeout",
    "sms_max_phone_attempts",
    "sms_code_retries_per_phone",
    "contribution_enabled",
    "contribution_server_url",
    "contribution_key",
    "contribution_mode",
    "custom_contribution_url",
    "custom_contribution_token",
]


class ConfigUpdate(BaseModel):
    data: dict


@router.get("")
def get_config():
    all_cfg = config_store.get_all()
    # 已删除渠道的 provider 名（LuckMail / 各临时邮箱 / 远程 icloud-hme /
    # applemail）已在 `config_store.get_all()` 里收敛成空串 —— 那是所有读取方
    # 的必经之路。这里**不再补默认值**：用户要求「留空，注册任务里必须显式
    # 选」，补一个兜底 provider 会在用户不知情的情况下把任务指到某个号池。
    all_cfg["mail_import_source"] = normalize_mail_import_source(
        all_cfg.get("mail_import_source"),
        all_cfg.get("mail_provider"),
    )
    if not all_cfg.get("outlook_backend"):
        all_cfg["outlook_backend"] = "graph"
    if not str(all_cfg.get("contribution_enabled", "") or "").strip():
        all_cfg["contribution_enabled"] = "0"
    if not all_cfg.get("contribution_server_url"):
        all_cfg["contribution_server_url"] = "http://new.xem8k5.top:7317/"
    if not all_cfg.get("contribution_mode"):
        all_cfg["contribution_mode"] = "codex"
    if not all_cfg.get("custom_contribution_url"):
        all_cfg["custom_contribution_url"] = "http://127.0.0.1:5000"
    if not str(all_cfg.get("sms_enabled", "") or "").strip():
        all_cfg["sms_enabled"] = "0"
    if not all_cfg.get("sms_provider"):
        all_cfg["sms_provider"] = "smsbower"
    if not all_cfg.get("sms_service"):
        all_cfg["sms_service"] = SMS_DEFAULT_SERVICE
    if not all_cfg.get("sms_country"):
        all_cfg["sms_country"] = SMS_DEFAULT_COUNTRY
    # 只返回已知 key，未设置的返回空字符串
    response = {k: all_cfg.get(k, "") for k in CONFIG_KEYS}
    # 口令键不回明文 —— 页面永远看不到已存的口令（「输入后不可查看」）。
    # 同时回 `<key>_set` 供界面显示「已配置」，否则用户无法判断库里到底有没有。
    for key in SECRET_CONFIG_KEYS:
        if key in response:
            response[key] = ""
    response.update(_secret_set_flags(all_cfg))
    return response


@router.put("")
def update_config(body: ConfigUpdate):
    # 只允许更新已知 key。**但不能静默丢弃**：前端字段名打错、或后端白名单
    # 漏加一个键时，接口照样返回 ok、前端弹「已保存」，实际什么都没写进去 ——
    # 实测就踩过：新增 `grok_register_mode` 时忘了加进 CONFIG_KEYS，面板选了
    # 「浏览器模式」保存成功却完全不生效，只能靠翻代码才发现。
    # 所以：仍容忍未知键（老前端/旧脚本会提交已删除渠道的值，直接 400 会连
    # 正常保存一起打断），但把丢弃的键回给调用方并记日志。
    safe = {k: v for k, v in body.data.items() if k in CONFIG_KEYS}
    ignored = sorted(k for k in body.data if k not in CONFIG_KEYS)
    if ignored:
        logger.warning("PUT /api/config 忽略了未知配置键: %s", ", ".join(ignored))
    # configs.value 是字符串列。前端若提交数组/对象（例如表单里的域名清单没转
    # JSON），SQLite 会抛 "type 'list' is not supported" → 500，用户看到的是
    # 整个保存失败且没有任何线索。这里统一转成字符串：list/dict 走 JSON，
    # bool 走小写（与库里既有的 "true"/"0" 一致，别写成 Python 的 "True"），
    # None 存空串，其余走 str()。
    for key, value in list(safe.items()):
        if isinstance(value, str):
            continue
        if value is None:
            safe[key] = ""
        elif isinstance(value, bool):
            safe[key] = "true" if value else "false"
        elif isinstance(value, (list, dict)):
            safe[key] = json.dumps(value, ensure_ascii=False)
        else:
            safe[key] = str(value)
    # 口令键的「留空 = 不修改」在服务端兜底。
    #
    # 前端已经会删掉空的 `<key>_set` 字段（它只是只读标记），但那只挡得住自家
    # 界面：脚本、老前端、手写 curl 都可能提交空串。而口令一旦被空串覆盖就
    # 静默丢失（用户下次注册才发现验证码解不了）。所以这里统一拦一道：
    #   * 空串 / 空值  → 不写（保持库里原值）
    #   * 显式 null    → 清空（上面已归一成 ""，这里放过）
    # 用「原样提交的键里是否显式出现 null」区分，而不是看归一后的值。
    cleared_secrets = {
        key for key, value in body.data.items() if key in SECRET_CONFIG_KEYS and value is None
    }
    for key in list(safe.keys()):
        if key in SECRET_CONFIG_KEYS and key not in cleared_secrets and not str(safe[key] or "").strip():
            safe.pop(key)
    # `<key>_set` 是只读标记，不接受写入（否则前端把只读标记提交回来会污染配置）
    for key in [k for k in safe if k.endswith(SECRET_SET_SUFFIX)]:
        safe.pop(key)
    if safe.get("mail_provider") is not None:
        # 已删除渠道的写入也要收敛（老前端缓存、旧脚本都可能还提交这些值）；
        # 只读侧收敛的话，库里会一直留着已删除的 provider 名。
        #
        # 用共享的 `normalize_mail_provider` 而不是写死比较：后者只认精确
        # 小写，实测 `PUT 'LuckMail'` / `' luckmail '` 都会按原样落库（读侧会
        # 收敛，所以没有功能故障，但库里留下了脏值）。helper 内部统一
        # lower + strip。收敛结果是空串，交由上层报「未配置邮箱服务」。
        safe["mail_provider"] = normalize_mail_provider(safe.get("mail_provider"))
    if "mail_import_source" in safe:
        # 只有同一次请求里带了 mail_provider 才谈得上对齐；面板单独改视图时，
        # 视图本身就是用户的新选择，不该被库里的旧 provider 拽回去
        safe["mail_import_source"] = (
            align_source_with_provider(safe["mail_import_source"], safe["mail_provider"])
            if "mail_provider" in safe
            else normalize_mail_import_source(safe["mail_import_source"])
        )
    config_store.set_many(safe)
    # 把被忽略的键回给调用方：前端可据此提示「这些字段没生效」，
    # 而不是弹一个「已保存」然后让用户困惑于配置为什么没变。
    result = {"ok": True, "updated": list(safe.keys())}
    if ignored:
        result["ignored"] = ignored
    return result
