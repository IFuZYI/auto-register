import os
import random
import string
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from sqlmodel import Session, select

from core.base_mailbox import OutlookMailbox
from core.db import AccountModel, OutlookAccountModel, current_engine, mailbox_pool_session

from .base import BaseMailImportStrategy
from .microsoft_import_rules import (
    ACCOUNT_TYPE_MICROSOFT_OAUTH,
    AutoDetectRowParser,
    DuplicateMicrosoftMailboxRule,
    MicrosoftMailImportRecord,
    MailApiUrlFormatRule,
    MicrosoftMailImportRuleEngine,
    RegisteredMicrosoftMailboxRule,
    normalize_import_email,
)
from .schemas import (
    MailImportBatchDeleteRequest,
    MailImportExecuteRequest,
    MailImportDeleteRequest,
    MailImportProviderDescriptor,
    MailImportResponse,
    MailImportSnapshot,
    MailImportSnapshotItem,
    MailImportSnapshotRequest,
    MailImportSummary,
)


def _utcnow():
    return datetime.now(timezone.utc)


def _known_platform_names() -> set[str]:
    """所有注册平台名（用于判定「对每个平台都注册过」）。"""
    names: set[str] = set()
    try:
        from core.registry import SUPPORTED_PLATFORMS

        names = {str(n).strip().lower() for n in SUPPORTED_PLATFORMS if str(n).strip()}
    except Exception:
        names = set()
    for fallback in ("chatgpt", "grok"):
        names.add(fallback)
    return names


class MicrosoftMailImportStrategy(BaseMailImportStrategy):
    @staticmethod
    def _generate_alias_email(email: str) -> str:
        local, domain = str(email or "").split("@", 1)
        base_local = local.split("+", 1)[0]
        suffix = "".join(random.choices(string.ascii_lowercase, k=6))
        return f"{base_local}+{suffix}@{domain}"

    @staticmethod
    def _expand_records_with_aliases(
        records: list[MicrosoftMailImportRecord],
        *,
        enabled: bool,
        alias_count: int,
        include_original: bool,
        taken_emails: set[str] | None = None,
    ) -> list[MicrosoftMailImportRecord]:
        if not enabled:
            return records

        expanded: list[MicrosoftMailImportRecord] = []
        target_count = max(1, min(int(alias_count or 1), 5))
        # 别名是随机拼的，撞上池内已有或注册过的地址就得换一个再拼
        taken = {normalize_import_email(email) for email in taken_emails or set()}

        for record in records:
            emails: list[str] = []
            seen_emails: set[str] = set(taken)
            if include_original:
                emails.append(record.email)
                seen_emails.add(normalize_import_email(record.email))

            aliases: list[str] = []
            max_attempts = max(20, target_count * 20)
            attempts = 0
            while len(aliases) < target_count and attempts < max_attempts:
                candidate = MicrosoftMailImportStrategy._generate_alias_email(record.email)
                attempts += 1
                if normalize_import_email(candidate) in seen_emails:
                    continue
                seen_emails.add(normalize_import_email(candidate))
                aliases.append(candidate)

            emails.extend(aliases)
            if not emails:
                emails.append(record.email)

            for email in emails:
                expanded.append(
                    MicrosoftMailImportRecord(
                        line_number=record.line_number,
                        email=email,
                        password=record.password,
                        client_id=record.client_id,
                        refresh_token=record.refresh_token,
                        account_type=record.account_type,
                        mailapi_url=record.mailapi_url,
                    )
                )
        return expanded

    @staticmethod
    def _resolve_oauth_check_workers(total_records: int) -> int:
        default_workers = 8
        raw_value = str(os.getenv("MAIL_IMPORT_OAUTH_WORKERS", default_workers)).strip()
        try:
            configured = int(raw_value)
        except (TypeError, ValueError):
            configured = default_workers
        configured = max(1, min(configured, 32))
        return max(1, min(configured, max(total_records, 1)))

    @staticmethod
    def _evaluate_availability(record, mailbox: OutlookMailbox) -> dict[str, object]:
        if getattr(record, "account_type", ACCOUNT_TYPE_MICROSOFT_OAUTH) != ACCOUNT_TYPE_MICROSOFT_OAUTH:
            return {"ok": True, "message": "ok"}
        try:
            result = mailbox.probe_oauth_availability(
                email=record.email,
                client_id=record.client_id,
                refresh_token=record.refresh_token,
            )
        except Exception as exc:
            return {
                "ok": False,
                "message": f"行 {record.line_number}: 微软邮箱可用性检测异常: {exc}",
                "reason": "oauth_probe_exception",
            }

        if result.get("ok"):
            return {"ok": True, "message": "ok"}
        return {
            "ok": False,
            "message": f"行 {record.line_number}: {result.get('message') or '微软邮箱可用性检测未通过'}",
            "reason": result.get("reason", "oauth_token_failed"),
        }

    @property
    def descriptor(self) -> MailImportProviderDescriptor:
        return MailImportProviderDescriptor(
            type="microsoft",
            label="微软邮箱（Outlook / Hotmail，本地导入）",
            description="导入微软邮箱本地账号池，运行时从数据库取账号并通过 Graph / IMAP 策略轮询邮件（默认 Graph）。",
            helper_text="支持两种格式并自动识别：1) 邮箱----密码----client_id----refresh_token（微软 OAuth）；2) 邮箱----mailapi_url（MailAPI URL 轮询取码）。",
            content_placeholder=(
                "example@outlook.com----password----client_id----refresh_token\n"
                "example@hotmail.com----password----client_id----refresh_token\n"
                "example@hotmail.com----https://mailapi.icu/key?type=html&orderNo=xxx"
            ),
            preview_empty_text="当前还没有已导入的微软邮箱本地账号。",
        )

    def get_snapshot(self, request: MailImportSnapshotRequest) -> MailImportSnapshot:
        with mailbox_pool_session("outlook") as session:
            accounts = session.exec(
                select(OutlookAccountModel).order_by(OutlookAccountModel.id)
            ).all()

        # 「这个地址注册过哪些平台」的权威证据在 `accounts` 表（跨库）。预览表
        # 要显示它、也要按平台筛选（一个邮箱可以被多个平台使用，只显示「已使用」
        # 区分不出是哪个平台用过）。一次批量查询，不是每行一次。
        registered: dict[str, set[str]] = {}
        try:
            from core.db import account_repository

            registered = account_repository.platforms_by_email(
                [account.email for account in accounts]
            )
        except Exception:  # noqa: BLE001 - 证据查询失败按无记录处理，不挡预览
            registered = {}

        limit = max(int(request.preview_limit or 0), 0)
        preview = accounts[:limit] if limit else []
        items = [
            MailImportSnapshotItem(
                index=idx,
                # 界面靠它把勾选的行映射回账号做「导入邮箱池」。没有 id 就只能
                # 按 email 反查，而 email 在池里虽然唯一、却可能带大小写差异。
                id=account.id,
                email=account.email,
                enabled=bool(account.enabled),
                status=str(getattr(account, "status", "available") or "available"),
                has_oauth=bool(
                    str(getattr(account, "account_type", ACCOUNT_TYPE_MICROSOFT_OAUTH) or ACCOUNT_TYPE_MICROSOFT_OAUTH)
                    == ACCOUNT_TYPE_MICROSOFT_OAUTH
                    and account.client_id
                    and account.refresh_token
                ),
                account_type=str(
                    getattr(account, "account_type", ACCOUNT_TYPE_MICROSOFT_OAUTH)
                    or ACCOUNT_TYPE_MICROSOFT_OAUTH
                ),
                used_platforms=str(getattr(account, "used_platforms", "") or ""),
                registered_platforms=sorted(
                    registered.get(normalize_import_email(account.email), set())
                ),
            )
            for idx, account in enumerate(preview, start=1)
        ]

        return MailImportSnapshot(
            type="microsoft",
            label=self.descriptor.label,
            count=len(accounts),
            items=items,
            truncated=len(accounts) > limit if limit > 0 else len(accounts) > 0,
        )

    def execute(self, request: MailImportExecuteRequest) -> MailImportResponse:
        lines = (request.content or "").splitlines()
        actionable_lines = [
            (idx, str(raw_line or "").strip())
            for idx, raw_line in enumerate(lines, start=1)
            if str(raw_line or "").strip() and not str(raw_line or "").strip().startswith("#")
        ]
        success = 0
        failed = 0
        errors: list[str] = []
        accounts: list[dict[str, object]] = []
        valid_records = []

        with mailbox_pool_session("outlook") as session:
            existing_emails = {
                normalize_import_email(email)
                for email in session.exec(select(OutlookAccountModel.email)).all()
                if str(email or "").strip()
            }

        # `accounts`（已注册账号）在**默认库**，`outlook_accounts` 在池库。
        # 两个查询必须各用各的会话：把 `select(AccountModel.email)` 塞进上面的池
        # 会话里，SQLite 会抛 `no such table: accounts` —— 因为池库那个文件里
        # 根本没有这张表，导入接口直接 500。
        #
        # 同时算一份「对**所有**平台都已注册」的名单：邮箱只对用掉它的那个
        # 平台一次性，所以「注册过 ChatGPT」不该阻止这个地址再给 Grok 用
        # （见 `RegisteredMicrosoftMailboxRule`）。
        with Session(current_engine()) as main_session:
            pairs = main_session.exec(
                select(AccountModel.email, AccountModel.platform)
            ).all()
        registered_emails = {
            normalize_import_email(email)
            for email, _platform in pairs
            if str(email or "").strip()
        }
        known_platforms = _known_platform_names()
        owners: dict[str, set[str]] = {}
        for email, platform in pairs:
            key = normalize_import_email(email)
            name = str(platform or "").strip().lower()
            if key and name:
                owners.setdefault(key, set()).add(name)
        fully_registered = {
            email for email, plats in owners.items() if plats >= known_platforms
        } if known_platforms else set()

        row_parser = AutoDetectRowParser()
        rule_engine = MicrosoftMailImportRuleEngine(
            rules=[
                DuplicateMicrosoftMailboxRule(),
                RegisteredMicrosoftMailboxRule(),
                MailApiUrlFormatRule(),
            ]
        )
        batch_seen_emails: set[str] = set()
        for line_number, line in actionable_lines:
            try:
                record = row_parser.parse(line_number, line)
            except ValueError as exc:
                failed += 1
                errors.append(str(exc))
                continue

            if normalize_import_email(record.email) in batch_seen_emails:
                failed += 1
                errors.append(f"行 {line_number}: 导入内容存在重复邮箱: {record.email}")
                continue
            batch_seen_emails.add(normalize_import_email(record.email))

            duplicate_check = rule_engine.evaluate(
                record,
                {
                    "existing_emails": existing_emails,
                    "registered_emails": registered_emails,
                    # 「对所有平台都注册过」的名单：只有这些才真的该拒绝入池
                    "registered_owner_emails": fully_registered,
                },
            )
            if not duplicate_check.get("ok"):
                failed += 1
                errors.append(str(duplicate_check.get("message") or f"行 {line_number}: 导入失败"))
                continue
            valid_records.append(record)

        alias_enabled = bool(request.alias_split_enabled)
        alias_count = int(request.alias_split_count or 5)
        alias_include_original = bool(request.alias_include_original)
        valid_records = self._expand_records_with_aliases(
            valid_records,
            enabled=alias_enabled,
            alias_count=alias_count,
            include_original=alias_include_original,
            taken_emails=existing_emails | registered_emails,
        )

        oauth_records = [
            record
            for record in valid_records
            if getattr(record, "account_type", ACCOUNT_TYPE_MICROSOFT_OAUTH)
            == ACCOUNT_TYPE_MICROSOFT_OAUTH
        ]
        oauth_check_results: dict[int, dict[str, object]] = {}
        if oauth_records:
            mailbox = OutlookMailbox()
            max_workers = self._resolve_oauth_check_workers(len(oauth_records))
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="oauth-import") as executor:
                future_map = {
                    executor.submit(self._evaluate_availability, record, mailbox): record
                    for record in oauth_records
                }
                for future in as_completed(future_map):
                    record = future_map[future]
                    try:
                        oauth_check_results[record.line_number] = future.result()
                    except Exception as exc:
                        oauth_check_results[record.line_number] = {
                            "ok": False,
                            "message": f"行 {record.line_number}: 微软邮箱可用性检测异常: {exc}",
                            "reason": "oauth_probe_exception",
                        }

        passed_records = []
        for record in valid_records:
            if getattr(record, "account_type", ACCOUNT_TYPE_MICROSOFT_OAUTH) != ACCOUNT_TYPE_MICROSOFT_OAUTH:
                passed_records.append(record)
                continue
            check_result = oauth_check_results.get(record.line_number) or {
                "ok": False,
                "message": f"行 {record.line_number}: 微软邮箱可用性检测未返回结果",
                "reason": "oauth_probe_missing_result",
            }
            if not check_result.get("ok"):
                failed += 1
                errors.append(str(check_result.get("message") or f"行 {record.line_number}: 导入失败"))
                continue
            passed_records.append(record)

        with mailbox_pool_session("outlook") as session:
            for record in passed_records:
                try:
                    account = OutlookAccountModel(
                        email=record.email,
                        password=record.password,
                        client_id=record.client_id,
                        refresh_token=record.refresh_token,
                        account_type=str(record.account_type or ACCOUNT_TYPE_MICROSOFT_OAUTH),
                        mailapi_url=str(record.mailapi_url or ""),
                        enabled=bool(request.enabled),
                        # 新导入的账号落 `unpooled`（未入池），不会立刻被注册任务领走。
                        # 用户明确要求「账号邮箱要选择导入才能导入邮箱池，不是全部导入」：
                        # 粘贴一屏邮箱的下一步是**在预览表里勾选**要用的那几个，而不是
                        # 让它们全部进入可领取状态。iCloud 别名池用的是同一套三态语义。
                        status="unpooled",
                        created_at=_utcnow(),
                        updated_at=_utcnow(),
                    )
                    session.add(account)
                    session.commit()
                    session.refresh(account)
                    existing_emails.add(normalize_import_email(record.email))
                    accounts.append({
                        "id": account.id,
                        "email": account.email,
                        "account_type": str(account.account_type or ACCOUNT_TYPE_MICROSOFT_OAUTH),
                        "has_oauth": bool(
                            str(account.account_type or ACCOUNT_TYPE_MICROSOFT_OAUTH) == ACCOUNT_TYPE_MICROSOFT_OAUTH
                            and account.client_id
                            and account.refresh_token
                        ),
                        "enabled": account.enabled,
                    })
                    success += 1
                except Exception as exc:
                    session.rollback()
                    failed += 1
                    errors.append(f"行 {record.line_number}: 创建失败: {str(exc)}")

        snapshot = self.get_snapshot(
            MailImportSnapshotRequest(
                type="microsoft",
                preview_limit=request.preview_limit,
            )
        )
        return MailImportResponse(
            type="microsoft",
            summary=MailImportSummary(
                total=success + failed,
                success=success,
                failed=failed,
            ),
            snapshot=snapshot,
            errors=errors,
            meta={
                "accounts": accounts,
                "alias_split_enabled": alias_enabled,
                "alias_split_count": alias_count,
                "alias_include_original": alias_include_original,
            },
        )

    def delete(self, request: MailImportDeleteRequest) -> MailImportResponse:
        email = str(request.email or "").strip()
        if not email:
            raise RuntimeError("缺少要删除的邮箱地址")

        with mailbox_pool_session("outlook") as session:
            account = session.exec(
                select(OutlookAccountModel).where(OutlookAccountModel.email == email)
            ).first()
            if not account:
                raise RuntimeError(f"未找到要删除的微软邮箱: {email}")

            session.delete(account)
            session.commit()

        snapshot = self.get_snapshot(
            MailImportSnapshotRequest(
                type="microsoft",
                preview_limit=request.preview_limit,
            )
        )
        return MailImportResponse(
            type="microsoft",
            summary=MailImportSummary(total=1, success=1, failed=0),
            snapshot=snapshot,
            meta={"deleted_email": email},
        )

    def batch_delete(self, request: MailImportBatchDeleteRequest) -> MailImportResponse:
        targets = [
            str(item.email or "").strip()
            for item in request.items
            if str(item.email or "").strip()
        ]
        deleted: list[str] = []
        errors: list[str] = []

        with mailbox_pool_session("outlook") as session:
            for email in targets:
                account = session.exec(
                    select(OutlookAccountModel).where(OutlookAccountModel.email == email)
                ).first()
                if not account:
                    errors.append(f"未找到要删除的微软邮箱: {email}")
                    continue
                session.delete(account)
                deleted.append(email)
            session.commit()

        snapshot = self.get_snapshot(
            MailImportSnapshotRequest(
                type="microsoft",
                preview_limit=request.preview_limit,
            )
        )
        return MailImportResponse(
            type="microsoft",
            summary=MailImportSummary(
                total=len(targets),
                success=len(deleted),
                failed=len(errors),
            ),
            snapshot=snapshot,
            errors=errors,
            meta={"deleted_emails": deleted},
        )
