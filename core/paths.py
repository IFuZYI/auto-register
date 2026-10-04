"""数据目录布局 —— 所有持久化路径的唯一真相源。

为什么要有这个模块
------------------
数据原先散落在仓库各处（根目录的 `account_manager.db`、`.secrets/`、`mail/`、
`services/external_logs/`…），既容易被 `git clean` 误删，也让备份/迁移/挂载卷
变成一件需要逐个目录去数的事。这里把布局集中定义一次，业务代码只引用本模块，
新增数据种类时也只改这一处。

目录布局
--------
::

    data/
    ├── account_manager.db        默认库：任务、日志、代理、全局配置
    ├── platforms/                平台分库（DATABASE_URL_<PLATFORM> 指到这里）
    │   └── <platform>.db
    ├── secrets/
    │   └── credential_key        凭据加密密钥（AES-GCM）
    └── logs/                     应用自身日志（Turnstile solver 等）

覆盖方式
--------
所有默认值都可被环境变量覆盖，容器里挂卷时只挂 `data/` 一个点即可：

- `DATA_DIR`：整个数据根（默认 `<仓库根>/data`）
- 其余路径默认派生自 `DATA_DIR`；`DATABASE_URL` 等既有变量优先级更高，
  见 `core/db/base.py`。

旧布局迁移
----------
`migrate_legacy_paths()` 在启动时把旧位置的文件搬到新位置。它**只搬不删**，
同名时保留新位置的文件不动（绝不覆盖真实数据），冲突项留在旧位置由使用者
自行清理 —— 数据迁移里"少做一步"比"做错一步"便宜得多。

注意：搬目录时目标目录往往已被 `ensure_data_dirs()` 建好，此时不能直接
`shutil.move`，否则源目录会被塞进目标**内部**多出一层，见 `_merge_dir()`。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

__all__ = [
    "DATA_DIR",
    "DEFAULT_DB_FILE",
    "PLATFORM_DB_DIR",
    "SECRETS_DIR",
    "CREDENTIAL_KEY_FILE",
    "LOG_DIR",
    "ensure_data_dirs",
    "migrate_legacy_paths",
]


def project_root() -> Path:
    """仓库根目录（`core/` 的上一级）。"""
    return Path(__file__).resolve().parents[1]


def _resolve_dir(raw: str, default: Path) -> Path:
    text = str(raw or "").strip()
    if not text:
        return default
    path = Path(text).expanduser()
    return path if path.is_absolute() else project_root() / path


def _env(name: str) -> str:
    return str(os.getenv(name, "") or "").strip()


# --------------------------------------------------------------------- 布局

DATA_DIR = _resolve_dir(_env("DATA_DIR"), project_root() / "data")

#: 默认库（任务/日志/代理/配置）。相对路径交给 SQLAlchemy 解析时会按
#: 进程工作目录解析，所以这里给出绝对路径，避免"从别处启动就换个库"。
DEFAULT_DB_FILE = DATA_DIR / "account_manager.db"

#: 平台分库目录（`DATABASE_URL_<PLATFORM>` 指到这里）
PLATFORM_DB_DIR = DATA_DIR / "platforms"

#: 默认就分库的平台 → 库文件名。
#:
#: 这两个是「邮箱来源」而不是注册平台：Outlook 号池与 iCloud 主号/别名会被多个
#: 平台的注册流程共用。它们的数据量、备份节奏、清理方式都和账号表不同，混在默认
#: 库里意味着「清空邮箱池」要写一条跨表 SQL，而单独成库只要删一个文件。
#: 注册平台（chatgpt / grok）不在此列 —— 它们默认仍与默认库共库，需要时用
#: `DATABASE_URL_<PLATFORM>` 单独指定。
DEFAULT_PLATFORM_DB_FILES: dict[str, str] = {
    "icloud": "icloud.db",
    "outlook": "outlook.db",
}

SECRETS_DIR = DATA_DIR / "secrets"
CREDENTIAL_KEY_FILE = SECRETS_DIR / "credential_key"

#: 应用自身日志（Turnstile solver 等）
LOG_DIR = DATA_DIR / "logs"


def ensure_data_dirs() -> None:
    """建齐所有数据子目录（幂等，启动时调用一次）。"""
    for directory in (
        DATA_DIR,
        PLATFORM_DB_DIR,
        SECRETS_DIR,
        LOG_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------------- 迁移

#: (旧路径, 新路径) 迁移表。旧路径相对仓库根。
_LEGACY_MOVES: tuple[tuple[Path, Path], ...] = (
    (project_root() / "account_manager.db", DEFAULT_DB_FILE),
    (project_root() / ".secrets" / "credential_key", CREDENTIAL_KEY_FILE),
    (project_root() / "services" / "turnstile_solver" / "solver.log", LOG_DIR / "solver.log"),
)


def _migrate_path(source: Path, target: Path) -> str:
    """搬一个文件或目录；返回人类可读的结果描述。

    目标已存在时不覆盖 —— 新位置的数据永远优先于旧位置的残留。
    """
    if not source.exists() or source == target:
        return ""

    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        if source.is_dir() and target.is_dir():
            return _merge_dir(source, target)
        return f"跳过 {source.name}：{target} 已存在（旧文件保留）"

    try:
        shutil.move(str(source), str(target))
    except (OSError, shutil.Error) as exc:
        return f"失败 {source} → {target}：{exc}"

    return f"{source} → {target}"


def _merge_dir(source: Path, target: Path) -> str:
    """把旧目录的内容并进已存在的目标目录，返回结果描述。

    这里不能直接 `shutil.move(source, target)`：目标目录已存在时 move 会把源
    整个塞到目标**内部**（变成 `target/<source名>/…`），数据看着像搬成功了，
    实际散在一个多余的层级下。逐项合并才是"搬进目标"，且同名文件保留目标。
    """
    moved, skipped = 0, 0
    for item in source.iterdir():
        destination = target / item.name
        if destination.exists():
            skipped += 1
            continue
        try:
            shutil.move(str(item), str(destination))
            moved += 1
        except (OSError, shutil.Error):
            skipped += 1

    # 全搬空才删旧目录；还有跳过的文件就留着，别让用户以为已经清理干净
    try:
        if not any(source.iterdir()):
            source.rmdir()
    except OSError:
        pass

    note = f"{source} → {target}（合并 {moved} 项）"
    if skipped:
        note += f"；{skipped} 项因同名保留在 {source}"
    return note


def migrate_legacy_paths() -> list[str]:
    """把旧布局的数据搬到 `data/` 下，返回做过的事（空列表＝无事可做）。

    只搬不删：目标已存在时保留目标、旧文件原地不动，由使用者自行清理。
    迁移失败不抛异常 —— 数据目录有问题时应用仍应能起来并把问题说清楚。
    """
    ensure_data_dirs()

    results: list[str] = []
    for source, target in _LEGACY_MOVES:
        outcome = _migrate_path(source, target)
        if outcome:
            results.append(outcome)
    return results
