"""数据与配置的一键导出 / 导入（服务迁移用）。

打包内容（单个 ZIP）
--------------------
- ``manifest.json``：格式版本、导出时间、应用版本、逐库 sha256 与行数统计
- ``account_manager.db``：默认库（任务 / 日志 / 代理 / 配置）—— SQLite
  用 ``VACUUM INTO`` 导出**一致性快照**，不是直接拷文件（运行中的库文件
  可能正处在 WAL 或半写状态，拷出来的副本有撕裂风险）
- ``platforms/*.db``：平台分库（邮箱池等）
- ``secrets/credential_key``：凭据加密密钥。**必须一起导** —— 账号凭据
  （iCloud 的 ``credentials_cipher`` 等）是用它加密的，只导库不导密钥，
  导入端所有加密字段都解不开，症状是「数据都在但全用不了」

导入语义（安全第一）
--------------------
1. **先校验后落盘**：ZIP 结构、manifest、每个 .db 的 sha256 全部验证通过
   才动现有数据 —— 半途失败不留「一半新一半旧」的混合状态。
2. **导入前自动备份**：当前 data/ 里的库与密钥先快照到
   ``data/import_backups/<时间戳>-<随机后缀>-<原因>/``（导入场景原因为 ``import``），
   导入失败或导入后想回退都能捞回来。
3. **导入后重载引擎**：SQLite 引擎有连接池，直接替换文件后旧连接仍指向
   旧 inode。这里显式 ``dispose`` 所有引擎并让注册表重新解析，使新库生效，
   无需重启进程。
4. **运行中任务拒绝导入**：有 pending/running 注册任务时导入会把它们
   脚下的库换掉 —— 直接 409 拒绝，让用户先停任务。

不做的事（明确的取舍）
----------------------
- 不导出 ``data/logs/``（应用日志，可丢弃，见 docs/DATA_DIRECTORY.md）
- 不导出 ``data/import_backups/``（避免备份套备份无限膨胀）
- 不做跨版本 schema 迁移：导入后跑一遍 ``run_migrations()``，
  老库缺列会自动补上；更早的格式版本直接拒绝（manifest 里记 version）。
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import secrets
import shutil
import sqlite3
import tempfile
import threading
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: 导入 / 导出 / 备份互斥锁。
#:
#: 「备份 → dispose 引擎 → 原子替换」这段窗口里两个并发导入会互相踩：
#: 各自备份到一半、换掉对方刚换的库，轻则备份互相覆盖，重则数据目录停在
#: 半新半旧。重活都在线程池里跑（同进程），进程内一把互斥锁即可；
#: 导出与导入共用同一把 —— 导出要读一致快照，导入会换文件，两者并行
#: 也可能把半旧数据打进包里。
_BUNDLE_LOCK = threading.RLock()

#: 导出包格式版本。不兼容变更（改了目录结构/语义）时 +1，
#: 导入侧对不认识的高版本直接拒绝，避免「以为导进去了实际半截」。
BUNDLE_FORMAT_VERSION = 1

#: ZIP 里各成员的固定路径。名字全部派生自 ``core/paths.py``（布局的
#: 唯一真相源）—— 在那里改名时这里跟着走，不会出现「导出包里的成员名
#: 和真实文件对不上」的静默失配。
from core.paths import CREDENTIAL_KEY_FILE as _KEY_FILE
from core.paths import DEFAULT_DB_FILE as _DEFAULT_DB_FILE
from core.paths import PLATFORM_DB_DIR as _PLATFORM_DB_DIR

MANIFEST_NAME = "manifest.json"
DEFAULT_DB_NAME = _DEFAULT_DB_FILE.name
PLATFORM_DIR_NAME = _PLATFORM_DB_DIR.name
KEY_FILE_NAME = _KEY_FILE.name
BACKUP_DIR_NAME = "import_backups"

#: 单个 .db 的大小上限（导入时防 zip 炸弹 / 误传大文件）。
#: 账号库正常在几十 MB 量级，留足余量。
MAX_DB_BYTES = 512 * 1024 * 1024
#: 整个导入包的压缩后大小上限。
MAX_BUNDLE_BYTES = 2 * 1024 * 1024 * 1024


class BundleError(RuntimeError):
    """导出/导入包的格式或内容问题（消息直接给用户看）。

    `status_code` 是建议的 HTTP 状态码：默认 400（包本身的问题）；
    运行中任务拒绝这类**状态冲突**用 409（前端与模块 docstring 按 409 写）。
    """

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = int(status_code)


#: SQLite 文件头（16 字节魔数）。sha256 只能证明「与 manifest 一致」，
#: 不能证明成员是合法 SQLite —— 被篡改过的 manifest 可以指向任意内容。
_SQLITE_MAGIC = b"SQLite format 3\x00"


# --------------------------------------------------------------------- 工具


def _sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sqlite_snapshot(source: Path, target: Path) -> None:
    """把 source 这个 SQLite 库导出成 target 处的一致性快照。

    用 ``VACUUM INTO``：SQLite 会把**当前已提交状态**写成一个全新的紧凑库，
    期间读锁保证不撕裂。直接 ``shutil.copy`` 在库正被写（WAL / journal 活跃）
    时可能拷到半个事务，导入端拿到损坏库。

    以**只读模式**打开源库：读写模式在源文件于检查与快照之间被删/被换时会
    静默新建一个空库，VACUUM INTO 产出 0 表的"合法"空快照 —— 静默数据丢失。
    只读模式直接抛 OperationalError，不会伪造空库。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    # VACUUM INTO 的目标路径不能已存在；用参数绑定而不是 f-string 拼 SQL
    uri = source.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("VACUUM INTO ?", (str(target),))
    finally:
        conn.close()


def _count_rows(db_path: Path) -> dict[str, int]:
    """各表行数（manifest 里记一份，导入后可比对）。"""
    counts: dict[str, int] = {}
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        for (name,) in rows:
            try:
                counts[str(name)] = int(
                    conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                )
            except sqlite3.Error:
                continue
    finally:
        conn.close()
    return counts


# --------------------------------------------------------------------- 导出


def _default_db_path() -> Optional[Path]:
    """默认库的**实际**文件路径。

    通常就是 ``data/account_manager.db``，但 ``DATABASE_URL`` 可以指到别处
    （测试隔离、自定义部署）。导出/备份必须跟随实际路径 —— 只看
    ``DEFAULT_DB_FILE`` 会导出一个不存在的文件，或在自定义部署下漏掉真正的库。
    非 SQLite（如 PostgreSQL）返回 None：那种部署不在本功能的覆盖范围内。
    """
    from core.db import current_engine

    try:
        url = current_engine().url
    except Exception:  # noqa: BLE001 - 引擎不可用不该让导出直接崩
        return None
    try:
        if url.get_backend_name() != "sqlite":
            return None
    except Exception:  # noqa: BLE001
        return None
    database = str(url.database or "")
    if not database or database == ":memory:":
        return None
    return Path(database)


def _credential_key_file() -> Path:
    """凭据密钥文件的**实际**路径。

    跟随 `CREDENTIAL_ENCRYPTION_KEY_FILE` 覆盖（`SecretBox` 的解析规则）——
    直接用 `core.paths.CREDENTIAL_KEY_FILE` 常量在自定义部署下会导错/漏导
    文件（症状：「导入成功但凭据全用不了」）。
    """
    from core.secret_box import key_file_path

    return key_file_path()


def _collect_export_files() -> tuple[list[tuple[Path, str]], list[tuple[Path, str]]]:
    """返回 (要打包的库文件, 其它文件) —— 元素是 (真实路径, ZIP 内路径)。"""
    from core.paths import DEFAULT_DB_FILE, PLATFORM_DB_DIR

    dbs: list[tuple[Path, str]] = []
    others: list[tuple[Path, str]] = []

    default_db = _default_db_path()
    if default_db is None or not default_db.exists():
        # 引擎路径取不到时退回约定位置（自定义部署下前者才是对的）
        default_db = DEFAULT_DB_FILE
    if default_db.exists():
        dbs.append((default_db, DEFAULT_DB_NAME))

    if PLATFORM_DB_DIR.exists():
        for path in sorted(PLATFORM_DB_DIR.glob("*.db")):
            dbs.append((path, f"{PLATFORM_DIR_NAME}/{path.name}"))

    key_file = _credential_key_file()
    if key_file.exists():
        others.append((key_file, f"secrets/{KEY_FILE_NAME}"))

    # 明确排除：日志（可丢弃）、备份目录（防套娃）。
    # 这两个目录在 data/ 下但都不该进导出包 —— 见模块 docstring。
    return dbs, others


def build_export_bundle() -> tuple[bytes, str]:
    """生成导出包。

    返回 ``(zip 字节, 建议文件名)``。SQLite 库先快照到临时目录再打包 ——
    直接打包运行中的库文件同样有一致性问题。
    """
    from core.paths import DATA_DIR

    dbs, others = _collect_export_files()
    if not dbs:
        raise BundleError("没有可导出的数据库 —— data/ 下没有 account_manager.db")

    manifest_dbs: list[dict[str, Any]] = []
    # 与导入共用互斥锁：导出读一致快照，导入会换库文件，并行时包里可能
    # 混入半旧数据（见 _BUNDLE_LOCK）。
    with _BUNDLE_LOCK, tempfile.TemporaryDirectory(prefix="export-bundle-") as tmp:
        tmp_dir = Path(tmp)
        buffer = io.BytesIO()

        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for source, arcname in dbs:
                snapshot = tmp_dir / Path(arcname).name
                try:
                    _sqlite_snapshot(source, snapshot)
                except sqlite3.Error as exc:
                    raise BundleError(
                        f"数据库快照失败 {source.name}: {exc}"
                    ) from exc
                zf.write(snapshot, arcname)
                manifest_dbs.append(
                    {
                        "name": arcname,
                        "sha256": _sha256_file(snapshot),
                        "size": snapshot.stat().st_size,
                        "tables": _count_rows(snapshot),
                    }
                )

            for source, arcname in others:
                zf.write(source, arcname)

            manifest = {
                "format_version": BUNDLE_FORMAT_VERSION,
                "exported_at": _utc_iso(),
                "data_dir": str(DATA_DIR),
                "databases": manifest_dbs,
                "includes": [arc for _, arc in others],
                "notes": (
                    "账号凭据用 secrets/credential_key 加密；导入端会一并恢复该密钥，"
                    "否则加密字段无法解密。日志与历史备份不在包内。"
                ),
            }
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))

        filename = f"register-backup-{_utc_stamp()}.zip"
        return buffer.getvalue(), filename


def collect_bundle_info() -> dict[str, Any]:
    """导出前预览：包里会有哪些库、各自多大、当前是否适合导入。

    API 层（`api/backup.py::backup_info`）调这个函数而不是自己拼 ——
    预览与导出/备份共用 `_collect_export_files` 的同一套枚举，
    三者不会各自漂移（新增数据种类只改一处）。
    """
    from core.paths import CREDENTIAL_KEY_FILE, DATA_DIR

    dbs, others = _collect_export_files()

    items: list[dict[str, Any]] = []
    total = 0
    for source, arcname in dbs:
        size = source.stat().st_size
        total += size
        label = (
            "默认库（任务 / 日志 / 代理 / 配置）"
            if arcname == DEFAULT_DB_NAME
            else "平台分库"
        )
        entry: dict[str, Any] = {
            # 显示名用包内名（与导出 ZIP 里的成员名一致，部署位置变了也不变）
            "name": arcname,
            "source": str(source),
            "path": label,
            "size": size,
        }
        # 行数是辅助信息：某个库损坏时不要让整个预览 500 —— 标 null 继续。
        try:
            entry["tables"] = _count_rows(source)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 %s 行数失败（预览继续）: %s", source, exc)
            entry["tables"] = None
        items.append(entry)

    return {
        "data_dir": str(DATA_DIR),
        "databases": items,
        "total_size": total,
        "has_credential_key": _credential_key_file().exists(),
        "has_active_tasks": _has_active_tasks(),
    }


# --------------------------------------------------------------------- 导入


def _read_manifest(zf: zipfile.ZipFile) -> dict[str, Any]:
    try:
        raw = zf.read(MANIFEST_NAME)
    except KeyError as exc:
        raise BundleError(f"导入包缺少 {MANIFEST_NAME} —— 不是本应用导出的备份") from exc
    except zipfile.BadZipFile as exc:
        raise BundleError(f"{MANIFEST_NAME} 数据损坏（CRC 校验失败）") from exc
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BundleError(f"{MANIFEST_NAME} 不是合法 JSON") from exc
    if not isinstance(manifest, dict):
        raise BundleError(f"{MANIFEST_NAME} 结构不对（应为对象）")

    version = manifest.get("format_version")
    try:
        version_int = int(str(version))
    except (TypeError, ValueError) as exc:
        raise BundleError("导入包缺少 format_version") from exc
    if version_int > BUNDLE_FORMAT_VERSION:
        raise BundleError(
            f"导入包格式版本 {version_int} 高于本程序支持的 {BUNDLE_FORMAT_VERSION} "
            "—— 请先升级本程序再导入"
        )
    return manifest


def _safe_member_name(name: str) -> str:
    """ZIP 成员名净化：拒绝绝对路径与 ``..``（zip slip）。"""
    text = str(name or "").replace("\\", "/").strip()
    if not text or text.startswith("/") or ".." in text.split("/"):
        raise BundleError(f"导入包含非法路径: {name!r}")
    return text


def _verify_members(zf: zipfile.ZipFile, manifest: dict[str, Any]) -> dict[str, dict]:
    """逐个核对 manifest 里登记的库：存在、大小、sha256。"""
    infos = {info.filename: info for info in zf.infolist()}
    verified: dict[str, dict] = {}
    for entry in manifest.get("databases") or []:
        if not isinstance(entry, dict):
            continue
        name = _safe_member_name(str(entry.get("name") or ""))
        info = infos.get(name)
        if info is None:
            raise BundleError(f"导入包缺少登记的数据库: {name}")
        if info.file_size > MAX_DB_BYTES:
            raise BundleError(f"数据库 {name} 超过大小上限（{info.file_size} 字节）")
        expected = str(entry.get("sha256") or "")
        if not expected:
            raise BundleError(f"manifest 里 {name} 缺 sha256")
        try:
            data = zf.read(name)
        except (zipfile.BadZipFile, zlib.error) as exc:
            # 成员数据损坏（CRC 不符/截断/解压失败）：不包装的话会穿透到
            # API 层变成 500 的英文底层异常，用户以为服务端坏了。
            raise BundleError(f"数据库 {name} 数据损坏（解压或 CRC 校验失败）") from exc
        actual = _sha256_bytes(data)
        if actual != expected:
            raise BundleError(
                f"数据库 {name} 校验失败（sha256 不一致）—— 文件损坏或被改动过"
            )
        if not data.startswith(_SQLITE_MAGIC):
            raise BundleError(
                f"数据库 {name} 不是合法的 SQLite 库 —— 拒绝导入（避免替换出坏库）"
            )
        verified[name] = {"bytes": data}
    if not verified:
        raise BundleError("导入包里没有登记任何数据库")
    return verified


def _backup_current_data(reason: str = "import") -> Optional[Path]:
    """把当前 data/ 的库与密钥备份一份（导入前自动调用）。

    返回备份目录；没有任何可备份内容时返回 None。

    目标枚举与导出共用 `_collect_export_files` —— 「什么算要备份的数据」
    只有一份定义，新增数据种类时导出与备份不会各改各的。
    """
    from core.paths import DATA_DIR

    dbs, others = _collect_export_files()
    targets: list[tuple[Path, str]] = [*dbs, *others]
    if not targets:
        return None

    # 目录名加短随机后缀：只到秒的时间戳在同一秒内两次导入（双击、自动化
    # 重试）会命中同一目录，第二次快照静默覆盖第一次的回退点。
    backup_root = DATA_DIR / BACKUP_DIR_NAME / f"{_utc_stamp()}-{secrets.token_hex(3)}-{reason}"
    backup_root.mkdir(parents=True, exist_ok=True)
    for source, arcname in targets:
        destination = backup_root / arcname
        destination.parent.mkdir(parents=True, exist_ok=True)
        # 备份是「把当前状态原样留存」，这里用快照而不是直接拷：
        # 运行中的库直接拷可能撕裂，恢复出来的备份不可用等于没备份。
        if source.suffix == ".db":
            try:
                _sqlite_snapshot(source, destination)
                continue
            except sqlite3.Error as exc:
                logger.warning("备份 %s 快照失败，退回文件拷贝: %s", source, exc)
        shutil.copy2(source, destination)
    return backup_root


def _dispose_all_engines() -> None:
    """释放所有数据库连接（导入替换文件前必须做）。

    用 ``core.db.close_all()`` —— 同一件事不要两份实现（它会随 db 层
    新增引擎源一起演进）。
    """
    from core.db import close_all

    close_all()


def _rollback_import(
    backup_root: Optional[Path],
    imported: list[str],
    *,
    key_replaced: bool,
) -> bool:
    """把导入替换过的文件恢复到导入前状态。返回是否全部成功。

    恢复源是导入前自动备份的 ``backup_root``（`_backup_current_data`）。
    备份里没有的成员 = 导入前不存在 → 把导入写进去的删掉。

    失败路径上任何异常都不外抛：调用方需要的是「回滚成没成」这个布尔，
    以及自己把备份路径报给用户。
    """
    from core.paths import CREDENTIAL_KEY_FILE, DATA_DIR

    if not imported and not key_replaced:
        return True

    ok = True

    def _restore_one(arcname: str, target: Path) -> None:
        nonlocal ok
        backup_file = (backup_root / arcname) if backup_root is not None else None
        try:
            if backup_file is not None and backup_file.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(backup_file), str(target))
            elif target.exists():
                # 导入前没有这个文件 → 把导入写进去的删掉
                target.unlink()
        except Exception as exc:  # noqa: BLE001 - 回滚尽力而为，失败要报告
            ok = False
            logger.error("回滚 %s 失败: %s", target, exc)

    for name in imported:
        if name == DEFAULT_DB_NAME:
            target = _default_db_path() or (DATA_DIR / name)
        else:
            target = DATA_DIR / name
        _restore_one(name, target)

    if key_replaced:
        _restore_one(f"secrets/{KEY_FILE_NAME}", _credential_key_file())
        try:
            from core.secret_box import secret_box

            secret_box.reset()
        except Exception as exc:  # noqa: BLE001
            logger.warning("回滚后重置密钥缓存失败: %s", exc)

    # 恢复后的库要重新挂上：dispose + init_db，进程内即可继续用旧数据。
    try:
        _dispose_all_engines()
        from core.db import init_db

        init_db()
    except Exception as exc:  # noqa: BLE001
        ok = False
        logger.error("回滚后重载数据库失败: %s", exc)

    return ok


def _has_active_tasks() -> bool:
    # 走公开包装器而不是直接伸进 `_task_store` 单例：任务存储将来若加
    # 门控（这正是该守卫的使用模式），私有直连会静默漂移。
    from api.tasks import has_active_register_task

    return bool(has_active_register_task())


def apply_import_bundle(bundle_bytes: bytes, *, filename: str = "") -> dict[str, Any]:
    """校验并应用导入包。

    返回结果 dict（含导入的库、行数、备份路径），失败抛 :class:`BundleError`。

    顺序刻意如此：**全部校验通过之后**才碰现有数据；替换前先自动备份。
    """
    if not bundle_bytes:
        raise BundleError("导入包为空")
    if len(bundle_bytes) > MAX_BUNDLE_BYTES:
        raise BundleError("导入包超过大小上限")

    try:
        zf = zipfile.ZipFile(io.BytesIO(bundle_bytes))
    except zipfile.BadZipFile as exc:
        raise BundleError("不是合法的 ZIP 文件") from exc

    # 互斥：两个并发导入会在「备份 → 换库」窗口互相踩（见 _BUNDLE_LOCK）。
    with _BUNDLE_LOCK, zf:
        manifest = _read_manifest(zf)
        verified = _verify_members(zf, manifest)

        # 运行中任务会跨过「库被替换」的瞬间：直接拒绝，让用户先停。
        # 放在校验之后（包本身有问题时先报包的问题，信息更有用）。
        if _has_active_tasks():
            raise BundleError(
                "有运行中的注册任务 —— 请先停止任务再导入", status_code=409
            )

        from core.paths import DATA_DIR

        backup_root = _backup_current_data("import")

        # 先解包到临时目录，全部就位后再原子替换 ——
        # 中途磁盘写满等异常不会留下「一半新库 + 一半旧库」。
        staging = Path(tempfile.mkdtemp(prefix="import-staging-", dir=str(DATA_DIR)))
        #: 替换进度（供失败回滚用）：已换的库与密钥
        imported: list[str] = []
        key_replaced = False
        try:
            for name, payload in verified.items():
                staged = staging / name
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged.write_bytes(payload["bytes"])

            key_member = f"secrets/{KEY_FILE_NAME}"
            staged_key: Optional[Path] = None
            if key_member in zf.namelist():
                staged_key = staging / key_member
                staged_key.parent.mkdir(parents=True, exist_ok=True)
                staged_key.write_bytes(zf.read(key_member))

            # 替换前先断开所有连接（SQLite 打开的文件句柄在 Windows 上还会
            # 阻止替换；Linux 上虽可替换，但旧连接继续指向旧 inode，
            # 不 dispose 的话「导入成功但读到的还是旧数据」）。
            _dispose_all_engines()

            for name in verified:
                if name == DEFAULT_DB_NAME:
                    # 默认库落在 DATABASE_URL 指向的位置，而不是约定路径 ——
                    # 自定义部署（或测试隔离）下两者不同，写错地方等于没导入。
                    target = _default_db_path() or (DATA_DIR / name)
                else:
                    target = DATA_DIR / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(staging / name), str(target))
                imported.append(name)

            if staged_key is not None:
                key_target = _credential_key_file()
                key_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(staged_key), str(key_target))
                key_replaced = True
                # 密钥文件换了，内存里缓存的旧密钥必须丢弃 ——
                # 不重置的话新导入的密文全解不开（详见 SecretBox.reset）。
                from core.secret_box import secret_box

                secret_box.reset()

            # 新库生效：重新解析分库映射 + 跑迁移（老库缺列在这里补上）。
            # 放在 try 内：迁移一旦抛错，库文件已被换掉而引擎又没挂上，
            # 没有回滚的话整个面板就停在「坏库 + 无连接」状态。
            from core.db import init_db

            init_db()
        except Exception as exc:
            # 替换阶段任何失败 → 用导入前的自动备份整体回滚。
            # 回滚成不成都要把「去哪捞数据」告诉用户。
            rolled_back = _rollback_import(
                backup_root, imported, key_replaced=key_replaced
            )
            if isinstance(exc, BundleError):
                raise
            if rolled_back:
                raise BundleError(
                    f"导入失败，已回滚到导入前状态（备份：{backup_root}）: {exc}"
                ) from exc
            raise BundleError(
                f"导入失败且回滚不完整 —— 请手工用备份恢复：{backup_root}（原因: {exc}）"
            ) from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        # 对比视图的缓存里存的是**旧库**的账号对照 —— 不清的话导入后
        # 面板管理页还在展示导入前的数据，看着像没导入成功。
        try:
            from services.panel_comparison_cache import clear_cache

            clear_cache()
        except Exception as exc:  # noqa: BLE001 - 缓存清理失败不该让导入失败
            logger.warning("清空面板对比缓存失败: %s", exc)

        # 导入的库里可能有 pending/running 的历史任务快照 —— 它们在这个
        # 进程里没有对应的内存记录，第一次列任务时会被标记为「因重启中断」，
        # 与真实重启的行为一致，无需额外处理。

        # 行数直接用 manifest 里导出处记录的计数（导入后库 == 导出的库，
        # 两者一致）—— 不再对每个库重跑一遍全表 COUNT(*)。
        manifest_tables: dict[str, dict[str, int]] = {}
        for entry in manifest.get("databases") or []:
            if isinstance(entry, dict):
                name = str(entry.get("name") or "")
                tables = entry.get("tables")
                if name and isinstance(tables, dict):
                    manifest_tables[name] = tables
        row_counts = {name: manifest_tables.get(name, {}) for name in imported}

        return {
            "ok": True,
            "imported": imported,
            "key_restored": staged_key is not None,
            "backup": str(backup_root) if backup_root else "",
            "row_counts": row_counts,
            "manifest": {
                "format_version": manifest.get("format_version"),
                "exported_at": manifest.get("exported_at"),
            },
            "source_filename": filename,
        }
