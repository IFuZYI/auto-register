"""`core/env_utils.env_number` 的契约：**读不懂就用默认值，永远不抛**。

2026-11-04 独立审查（切片 D）：int 收口只捕 `TypeError/ValueError`，漏了
`OverflowError` —— `type(default)(inf)` 会抛，而本模块建立的全部意义就是
「env 写错不许在 import 期炸掉整个平台」。
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from core.env_utils import env_number


class EnvNumberNeverRaisesTests(unittest.TestCase):
    def _call(self, raw, default, **kw):
        import os
        with patch.dict(os.environ, {"__PROBE_ENV__": raw}):
            return env_number("__PROBE_ENV__", default, **kw)

    def test_int_default_survives_non_finite(self):
        for raw in ("inf", "Infinity", "1e999", "-inf", "nan"):
            with self.subTest(raw=raw):
                self.assertEqual(self._call(raw, 200), 200, f"{raw!r} 应当退回默认值")

    def test_int_default_is_still_int_for_a_normal_value(self):
        got = self._call("350", 200)
        self.assertEqual(got, 350)
        self.assertIsInstance(got, int)

    def test_missing_and_blank_use_the_default(self):
        import os
        with patch.dict(os.environ, {"__PROBE_ENV__": "   "}):
            self.assertEqual(env_number("__PROBE_ENV__", 200), 200)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("__PROBE_ENV__", None)
            self.assertEqual(env_number("__PROBE_ENV__", 200), 200)
