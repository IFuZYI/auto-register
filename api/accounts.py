from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select, func
from pydantic import BaseModel
from core.db import AccountModel, get_session
from services.account_export import (
    DEFAULT_EXPORT_FORMAT,
    export_filename,
    list_export_formats,
    render_accounts,
)
from services.chatgpt_account_state import filter_accounts_by_plus_status
from typing import Optional
from datetime import datetime, timezone
import io, csv, json, logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/accounts", tags=["accounts"])


class AccountCreate(BaseModel):
    platform: str
    email: str
    password: str
    status: str = "registered"
    token: str = ""
    cashier_url: str = ""


class AccountUpdate(BaseModel):
    status: Optional[str] = None
    token: Optional[str] = None
    cashier_url: Optional[str] = None


class ImportRequest(BaseModel):
    platform: str
    lines: list[str]
    #: 导入格式：`text`（每行 `email password [cashier_url]`）或
    #: `json`（导出格式的数组，全字段）。
    format: str = "text"


class BatchDeleteRequest(BaseModel):
    ids: list[int]
    # 账号 id 是每库自增的，指定 platform 才能保证不误删别的平台的同 id 账号
    platform: str = ""


class ExportTextRequest(BaseModel):
    """按格式导出：给了 ``account_ids`` 就只导这些，否则导当前筛选的全部。"""

    format: str = DEFAULT_EXPORT_FORMAT
    platform: str = ""
    account_ids: list[int] = []
    status: str = ""
    email: str = ""
    plus_status: str = ""
    created_at_start: Optional[datetime] = None
    created_at_end: Optional[datetime] = None


def _filtered_accounts(
    session: Session | None = None,
    *,
    platform: str = "",
    status: str = "",
    email: str = "",
    plus_status: str = "",
    created_at_start: Optional[datetime] = None,
    created_at_end: Optional[datetime] = None,
    limit: Optional[int] = None,
    offset: int = 0,
) -> list[AccountModel]:
    """按条件过滤账号（**跨库**）。

    分库后账号散落在各平台库，必须走仓储汇总；否则不指定平台时只能看到
    默认库里的账号。过滤条件（含 created_at）全部下推到 SQL。

    `plus_status` 存在 extra_json 里，SQL 筛不动，只能取出来再过——因此
    带 `plus_status` 时不能下推分页（否则会先截断再过滤，结果缺行），
    此时 `limit` 由调用方在过滤后自行施加。
    """
    from core.db import account_repository

    if plus_status:
        # extra_json 里的字段筛不动：全量取回后再过滤/分页
        rows = account_repository.list_all_accounts(
            platform=platform or "",
            status=status or "",
            email_contains=email or "",
            created_at_start=created_at_start,
            created_at_end=created_at_end,
        )
        return list(filter_accounts_by_plus_status(rows, plus_status))

    return account_repository.list_all_accounts(
        platform=platform or "",
        status=status or "",
        email_contains=email or "",
        created_at_start=created_at_start,
        created_at_end=created_at_end,
        limit=limit,
        offset=offset,
    )


def _count_accounts(
    *,
    platform: str = "",
    status: str = "",
    email: str = "",
    plus_status: str = "",
    created_at_start: Optional[datetime] = None,
    created_at_end: Optional[datetime] = None,
) -> int:
    """分页用的 total（跨库）。

    普通条件走 SQL COUNT；`plus_status` 需要读 extra_json，退回内存计数。
    """
    from core.db import account_repository

    if plus_status:
        return len(
            _filtered_accounts(
                platform=platform,
                status=status,
                email=email,
                plus_status=plus_status,
                created_at_start=created_at_start,
                created_at_end=created_at_end,
            )
        )

    return account_repository.count_all_accounts(
        platform=platform or "",
        status=status or "",
        email_contains=email or "",
        created_at_start=created_at_start,
        created_at_end=created_at_end,
    )


@router.get("")
def list_accounts(
    platform: Optional[str] = None,
    status: Optional[str] = None,
    email: Optional[str] = None,
    plus_status: Optional[str] = None,
    created_at_start: Optional[datetime] = None,
    created_at_end: Optional[datetime] = None,
    page: int = 1,
    page_size: int = 20,
):
    page = max(1, int(page or 1))
    page_size = max(1, int(page_size or 20))
    offset = (page - 1) * page_size

    plat = platform or ""
    stat = status or ""
    mail = email or ""
    plus = plus_status or ""

    total = _count_accounts(
        platform=plat,
        status=stat,
        email=mail,
        plus_status=plus,
        created_at_start=created_at_start,
        created_at_end=created_at_end,
    )

    if plus:
        # 内存过滤路径：取全量再切片（total 已由上面算过）
        rows = _filtered_accounts(
            platform=plat,
            status=stat,
            email=mail,
            plus_status=plus,
            created_at_start=created_at_start,
            created_at_end=created_at_end,
        )
        items = rows[offset : offset + page_size]
    else:
        items = _filtered_accounts(
            platform=plat,
            status=stat,
            email=mail,
            created_at_start=created_at_start,
            created_at_end=created_at_end,
            limit=page_size,
            offset=offset,
        )

    return {"total": total, "page": page, "items": items}


@router.post("")
def create_account(body: AccountCreate, session: Session = Depends(get_session)):
    """新增账号。同平台同邮箱已存在时更新而不是重复插入（邮箱是唯一键）。"""
    from core.base_platform import AccountStatus
    from core.db import account_repository, normalize_email

    email = normalize_email(body.email)
    if not email:
        raise HTTPException(400, "邮箱不能为空")

    # 状态精简的写侧兜底：旧客户端可能提交已删除的 trial / subscribed
    status = AccountStatus.normalize(body.status)
    existing = account_repository.find_by_email(body.platform, email)
    if existing is not None:
        existing.password = body.password or existing.password
        existing.token = body.token or existing.token
        existing.status = status
        existing.cashier_url = body.cashier_url or existing.cashier_url
        existing.updated_at = datetime.now(timezone.utc)
        with account_repository.session_for(body.platform) as sess:
            sess.add(existing)
            sess.commit()
            sess.refresh(existing)
        return existing

    acc = AccountModel(
        platform=body.platform,
        email=email,
        password=body.password,
        status=status,
        token=body.token,
        cashier_url=body.cashier_url,
    )
    with account_repository.session_for(body.platform) as sess:
        sess.add(acc)
        sess.commit()
        sess.refresh(acc)
    return acc


@router.get("/stats")
def get_stats(session: Session = Depends(get_session)):
    """统计各平台账号数量和状态分布。

    分库后账号散落在各平台库，仓储层负责跨库汇总（只看默认库会漏掉分库平台）。
    """
    from core.db import account_repository

    return account_repository.stats()


@router.get("/export")
def export_accounts(
    platform: Optional[str] = None,
    status: Optional[str] = None,
):
    accounts = _filtered_accounts(
        platform=platform or "",
        status=status or "",
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["platform", "email", "password", "user_id", "region",
                     "status", "cashier_url", "created_at"])
    for acc in accounts:
        writer.writerow([acc.platform, acc.email, acc.password, acc.user_id,
                         acc.region, acc.status, acc.cashier_url,
                         acc.created_at.strftime("%Y-%m-%d %H:%M:%S")])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=accounts.csv"}
    )


@router.get("/export-formats")
def get_export_formats():
    """导出格式清单。前端下拉框直接渲染这个列表，新增格式不用改前端。"""
    return {"formats": list_export_formats(), "default": DEFAULT_EXPORT_FORMAT}


@router.post("/export-text")
def export_accounts_text(body: ExportTextRequest, session: Session = Depends(get_session)):
    if body.account_ids:
        from core.db import account_repository

        ids = [int(v) for v in body.account_ids if int(v) > 0]
        rows = account_repository.get_many(ids)
        # 按前端给的顺序回填，勾选顺序就是导出顺序
        row_map = {row.id: row for row in rows}
        accounts = [row_map[i] for i in dict.fromkeys(ids) if i in row_map]
    else:
        accounts = _filtered_accounts(
            platform=body.platform,
            status=body.status,
            email=body.email,
            plus_status=body.plus_status,
            created_at_start=body.created_at_start,
            created_at_end=body.created_at_end,
        )

    try:
        content = render_accounts(accounts, body.format)
    except KeyError:
        raise HTTPException(400, f"未知的导出格式: {body.format}")

    return {
        "format": body.format,
        "total": len(accounts),
        "lines": len([line for line in content.split("\n") if line]) if content else 0,
        "content": content,
        "filename": export_filename(body.platform, body.format),
    }


@router.post("/import")
def import_accounts(
    body: ImportRequest,
    session: Session = Depends(get_session),
):
    """批量导入。

    - `format="text"`（默认）：每行 `email password [cashier_url]`
    - `format="json"`：导出格式的 JSON 数组，**全字段**（platform / email /
      password / totp_secret / access_token / refresh_token / id_token /
      session_token / status / created_at 等）

    邮箱是唯一业务键：同平台同邮箱已存在时更新而不是重复插入。

    JSON 导入是 `_render_json` 导出的逆向：那份导出写的字段（含 sso 等凭证）
    这里要能原样收回来，否则"导出备份 → 换台机器导入"会丢 token。
    """
    from core.db import account_repository, normalize_email

    if str(body.format or "").strip().lower() == "json":
        return _import_json_accounts(body, session)

    created = 0
    updated = 0
    skipped = 0
    for line in body.lines:
        parts = line.strip().split()
        if len(parts) < 2:
            # 行不成形（少于「邮箱 + 密码」两列）时计入 skipped —— 与 JSON 模式
            # 口径一致，否则前端「导入了多少、跳过多少」在两种格式下对不上。
            skipped += 1
            continue
        email = normalize_email(parts[0])
        if not email:
            skipped += 1
            continue
        password = parts[1]
        extra = parts[2] if len(parts) > 2 else ""
        if extra:
            try:
                json.loads(extra)
            except (json.JSONDecodeError, ValueError):
                extra = "{}"
        else:
            extra = "{}"

        existing = account_repository.find_by_email(body.platform, email)
        if existing is not None:
            existing.password = password or existing.password
            existing.extra_json = extra
            account_repository.upsert(existing)
            updated += 1
        else:
            account_repository.upsert(AccountModel(
                platform=body.platform, email=email,
                password=password, extra_json=extra,
            ))
            created += 1
    return {"created": created, "updated": updated, "skipped": skipped}


#: JSON 导入时进 `extra` 的凭证字段（导出侧 `_render_json` 写的那几个）。
#: `platform` / `email` / `password` / `status` / `created_at` 是账号表自己的列，
#: 不进 extra；其余都是 extra 键 —— 与导出字段一一对应，保证往返不丢。
#: **含 sso**（grok 主凭证）—— 整理前缺失导致 grok 账号往返丢 SSO。
_IMPORT_EXTRA_KEYS = (
    "totp_secret",
    "access_token",
    "refresh_token",
    "id_token",
    "session_token",
    "sso",
)


def _import_json_accounts(body: ImportRequest, session: Session) -> dict:
    """导入 JSON 数组（导出格式）。逐条 upsert，凭证进 extra。"""
    from core.db import account_repository, normalize_email

    # `lines` 是前端按行传的；JSON 模式下它承载整段文本（见前端 handleImport）。
    raw = "\n".join(body.lines).strip()
    if not raw:
        raise HTTPException(400, "导入内容为空")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(400, f"JSON 解析失败：{exc}") from exc
    if isinstance(payload, dict):
        # 容忍 `{"accounts": [...]}` 这类包装
        for key in ("accounts", "items", "data", "list"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        raise HTTPException(400, "JSON 导入需要一个数组（或含 accounts 数组的对象）")

    created = 0
    updated = 0
    skipped = 0
    for entry in payload:
        if not isinstance(entry, dict):
            skipped += 1
            continue
        email = normalize_email(str(entry.get("email") or ""))
        if not email:
            skipped += 1
            continue
        # 每条可以带自己的 platform，缺省用请求里的（导入界面选的平台）
        platform = str(entry.get("platform") or body.platform or "").strip()
        if not platform:
            skipped += 1
            continue

        existing = account_repository.find_by_email(platform, email)
        target = existing if existing is not None else AccountModel(
            platform=platform, email=email, password="",
        )

        # 账号表自己的列：只在新行或字段有值时写（空值不覆盖已有数据）
        password = str(entry.get("password") or "")
        if password or not existing:
            target.password = password
        for column in ("user_id", "region", "status", "cashier_url"):
            value = str(entry.get(column) or "")
            if value or not existing:
                setattr(target, column, value)

        # 凭证与平台字段进 extra：**只写有值的**，空值不覆盖
        # （导出的空字段若把已有 token 抹掉，导入就成了数据损失）
        extra = target.get_extra()
        for key in _IMPORT_EXTRA_KEYS:
            value = str(entry.get(key) or "").strip()
            if value:
                extra[key] = value
        # 导出没有、但导入方可能带的其它自定义字段也一并收下
        for key, value in entry.items():
            if key in _IMPORT_EXTRA_KEYS or key in (
                "platform", "email", "password", "user_id", "region",
                "status", "cashier_url", "created_at", "updated_at",
            ):
                continue
            if value not in (None, "", [], {}):
                extra[key] = value
        target.set_extra(extra)

        # created_at 由导出带回来时保留原值（排序/筛选依赖它）
        created_at = str(entry.get("created_at") or "").strip()
        if created_at and not existing:
            parsed = _parse_import_datetime(created_at)
            if parsed is not None:
                target.created_at = parsed

        account_repository.upsert(target)
        if existing is not None:
            updated += 1
        else:
            created += 1
    return {"created": created, "updated": updated, "skipped": skipped}


def _parse_import_datetime(value: str) -> Optional[datetime]:
    """解析导出里的时间串（`2026-03-31 04:10:00` 或 ISO）。失败返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    for candidate in (text, text.replace(" ", "T")):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


@router.post("/batch-delete")
def batch_delete_accounts(
    body: BatchDeleteRequest,
):
    """批量删除账号（跨库）"""
    from core.db import account_repository

    if not body.ids:
        raise HTTPException(400, "账号 ID 列表不能为空")

    if len(body.ids) > 1000:
        raise HTTPException(400, "单次最多删除 1000 个账号")

    deleted_count = 0
    not_found_ids = []

    try:
        deleted, not_found = account_repository.delete_many(body.ids, platform=body.platform)
        deleted_count = len(deleted)
        not_found_ids = not_found

        logger.info(f"批量删除成功: {deleted_count} 个账号")

        return {
            "deleted": deleted_count,
            "not_found": not_found_ids,
            "total_requested": len(body.ids)
        }
    except Exception as e:
        logger.exception("批量删除失败")
        raise HTTPException(500, f"批量删除失败: {str(e)}")


@router.post("/check-all")
def check_all_accounts(platform: Optional[str] = None,
                       background_tasks: BackgroundTasks = None):
    from core.scheduler import scheduler
    background_tasks.add_task(scheduler.check_accounts_valid, platform)
    return {"message": "批量检测任务已启动"}


@router.get("/{account_id}")
def get_account(account_id: int, platform: str = ""):
    from core.db import account_repository

    # 带上 platform 更准：账号 id 是每库自增的，不给平台时跨库扫到的可能是
    # 另一个平台的同 id 账号。前端列表本来就是按平台分的，顺手传上来即可。
    acc = account_repository.get(account_id, platform=platform)
    if not acc:
        raise HTTPException(404, "账号不存在")
    return acc


@router.patch("/{account_id}")
def update_account(account_id: int, body: AccountUpdate, platform: str = ""):
    from core.db import account_repository

    acc = account_repository.get(account_id, platform=platform)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if body.status is not None:
        from core.base_platform import AccountStatus

        # 状态精简的写侧兜底：旧客户端可能提交已删除的 trial / subscribed
        acc.status = AccountStatus.normalize(body.status)
    if body.token is not None:
        acc.token = body.token
    if body.cashier_url is not None:
        acc.cashier_url = body.cashier_url
    acc.updated_at = datetime.now(timezone.utc)
    # 走仓储 upsert 落库（此前直接引用了一个不存在的 `session` 变量 ——
    # 函数签名里没有、模块级也没绑定，实测 PATCH 必 500 NameError，
    # 详情弹窗保存 100% 失败。`upsert` 按 (平台, 邮箱) 定位现有行并保留
    # 未提交字段，与导入路径同款）。
    return account_repository.upsert(acc)


@router.delete("/{account_id}")
def delete_account(account_id: int, platform: str = ""):
    from core.db import account_repository

    # 同 get_account：带上 platform 才不会因 ID 撞号删掉别的平台的账号
    if not account_repository.delete(account_id, platform=platform):
        raise HTTPException(404, "账号不存在")
    return {"ok": True}


@router.post("/{account_id}/check")
def check_account(account_id: int, background_tasks: BackgroundTasks):
    from core.db import account_repository

    acc = account_repository.get(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    background_tasks.add_task(_do_check, account_id)
    return {"message": "检测任务已启动"}


def _do_check(account_id: int):
    from core.db import account_repository

    acc = account_repository.get(account_id)
    if acc:
        from core.base_platform import Account, RegisterConfig
        from core.registry import get
        try:
            PlatformCls = get(acc.platform)
            plugin = PlatformCls(config=RegisterConfig())
            obj = Account(platform=acc.platform, email=acc.email,
                         password=acc.password, user_id=acc.user_id,
                         region=acc.region, token=acc.token,
                         extra=acc.get_extra())
            valid = plugin.check_valid(obj)
            # 重新取一次：check_valid 期间可能已被别处改动
            fresh = account_repository.get(account_id)
            if fresh:
                if fresh.platform != "chatgpt":
                    fresh.status = fresh.status if valid else "invalid"
                fresh.updated_at = datetime.now(timezone.utc)
                account_repository.upsert(fresh)
        except Exception:
            logger.exception("检测账号 %s 时出错", account_id)
