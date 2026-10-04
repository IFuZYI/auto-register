"""`/api/runtime` 版本戳的契约：能识别「改了代码没重启」，且不被未跟踪文件污染。

背景（实测）：本服务手工拉起，改完代码不重启就跑旧代码，症状与「代码没改对」
一模一样。端点用 `code_version`（进程启动那刻算的戳）与 `disk_version`（此刻
磁盘的戳）是否相等来分辨这两件事。

两条必须成立的性质：

1. **未跟踪文件不参与判断。** 根目录长期存在未跟踪目录（`.hermes/` 之类的
   本地状态）时，若把它们算作「脏」，戳会被永久钉成 `+dirty` —— 此后改动
   已跟踪文件不再改变戳，`stale` 恒为 false，漏报的正是这个端点要防的情况。
2. **不同的脏状态要能区分。**「改了 → 启动 → 又改」两次都带 `+dirty`；若戳只
   标记「脏/不脏」而不含内容指纹，`stale` 会误报 false。

测试在临时 git 仓库上验证语义，不碰真实工作区。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _git(*args: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )


def _make_repo(base: Path) -> str:
    """建一个带一次提交的临时仓库，返回其路径。"""
    root = str(base)
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    _git("config", "commit.gpgsign", "false", cwd=root)
    (base / "code.py").write_text("v1\n", encoding="utf-8")
    _git("add", "code.py", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    return root


class RuntimeVersionStampTests(unittest.TestCase):
    def setUp(self):
        import main as main_mod

        self.main = main_mod
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = _make_repo(self.base)

    def tearDown(self):
        self._tmp.cleanup()

    def test_untracked_files_do_not_change_the_stamp(self):
        """未跟踪文件不该把版本戳钉成脏（否则 stale 检测被永久污染）。"""
        clean = self.main._read_git_version(self.root)
        (self.base / ".hermes").mkdir()
        (self.base / ".hermes" / "state.json").write_text("{}", encoding="utf-8")
        (self.base / "scratch.txt").write_text("x", encoding="utf-8")
        self.assertEqual(
            self.main._read_git_version(self.root), clean,
            "未跟踪文件改变了版本戳 → stale 会永久报脏或永久漏报",
        )

    def test_tracked_edit_changes_the_stamp(self):
        """改了已跟踪文件必须改变戳 —— 这是「改完没重启」检测的核心。"""
        clean = self.main._read_git_version(self.root)
        (self.base / "code.py").write_text("v2\n", encoding="utf-8")
        self.assertNotEqual(
            self.main._read_git_version(self.root), clean,
            "改了已跟踪文件却没改变版本戳 → 漏报未重启",
        )

    def test_distinct_dirty_states_are_distinguishable(self):
        """「改了 → 启动 → 又改」两个脏状态不能撞成同一个戳。"""
        (self.base / "code.py").write_text("v2\n", encoding="utf-8")
        first = self.main._read_git_version(self.root)
        (self.base / "code.py").write_text("v3\n", encoding="utf-8")
        second = self.main._read_git_version(self.root)
        self.assertNotEqual(
            first, second,
            "两个不同的脏状态撞成同一个戳 → 第二次改动会被漏报",
        )

    def test_stale_semantics_edit_after_clean_boot(self):
        """端到端语义：干净启动 → 改文件 → 两个戳必须不同（= stale）。"""
        boot = self.main._read_git_version(self.root)
        (self.base / "code.py").write_text("v2\n", encoding="utf-8")
        disk = self.main._read_git_version(self.root)
        self.assertNotEqual(boot, disk, "改完没重启没被识别出来")

    def test_stale_semantics_untracked_noise_does_not_raise_false_alarm(self):
        """只多了未跟踪文件：不该报 stale（否则「狼来了」没人再信这个标记）。"""
        boot = self.main._read_git_version(self.root)
        (self.base / ".hermes").mkdir()
        (self.base / ".hermes" / "x").write_text("x", encoding="utf-8")
        self.assertEqual(self.main._read_git_version(self.root), boot)

    def test_real_repo_stamp_tracks_only_tracked_changes(self):
        """真实仓库：戳的脏标记只跟已跟踪改动走，与 git 自己的口径一致。"""
        status = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True,
        )
        stamp = self.main._read_git_version(str(ROOT))
        if status.stdout.strip():
            self.assertIn("+dirty", stamp, "有已跟踪改动却没标脏")
        else:
            self.assertNotIn(
                "+dirty", stamp,
                "工作区没有已跟踪改动，戳却是脏的（未跟踪文件污染了它）",
            )


if __name__ == "__main__":
    unittest.main()
