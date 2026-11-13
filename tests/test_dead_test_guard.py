"""Guard: no test function may sit inside/after the `__main__` block.

The 2026-11 audit found a `test_export_only_keys_are_exactly_cookies` defined
after `unittest.main()`: it never ran and asserted an outdated tuple.  This
guard makes that pattern fail CI.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from scripts.check_dead_tests import find_dead_tests

TESTS_DIR = Path(__file__).resolve().parent


class DeadTestGuardTests(unittest.TestCase):
    def test_repo_tests_have_no_dead_definitions(self):
        for path in sorted(TESTS_DIR.rglob("*.py")):
            dead = find_dead_tests(path.read_text(encoding="utf-8"))
            self.assertEqual(dead, [], f"{path} 里有永远不会被收集的测试: {dead}")

    def test_flags_test_inside_the_main_guard(self):
        source = (
            'if __name__ == "__main__":\n'
            "    unittest.main()\n"
            "\n"
            "    def test_never_runs(self):\n"
            "        pass\n"
        )
        self.assertEqual(find_dead_tests(source), [("test_never_runs", 4)])

    def test_module_level_test_after_guard_is_still_collected(self):
        """守卫之后的模块级 def 会在 import 时定义，pytest 照常收集 —— 不是死测试。"""
        source = (
            'if __name__ == "__main__":\n'
            "    unittest.main()\n"
            "\n"
            "def test_after_guard(self):\n"
            "    pass\n"
        )
        self.assertEqual(find_dead_tests(source), [])

    def test_flags_test_nested_under_try_inside_guard(self):
        source = (
            'if __name__ == "__main__":\n'
            "    try:\n"
            "        def test_nested(self):\n"
            "            pass\n"
            "    except Exception:\n"
            "        pass\n"
        )
        self.assertEqual(find_dead_tests(source), [("test_nested", 3)])

    def test_flags_reversed_main_guard(self):
        source = (
            'if "__main__" == __name__:\n'
            "    def test_reversed(self):\n"
            "        pass\n"
        )
        self.assertEqual(find_dead_tests(source), [("test_reversed", 2)])

    def test_allows_tests_in_classes_and_before_the_guard(self):
        source = (
            "import unittest\n"
            "\n"
            "class T(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        pass\n"
            "\n"
            'if __name__ == "__main__":\n'
            "    unittest.main()\n"
        )
        self.assertEqual(find_dead_tests(source), [])


if __name__ == "__main__":
    unittest.main()
