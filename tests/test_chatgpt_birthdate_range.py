"""注册链路出生日期的年龄约束（18+）。

历史：这里原有两个测试，钉的是 `platforms.chatgpt.constants` 的
`generate_random_user_info` 与 `utils.generate_random_birthday` —— 两者
都是**从未被调用**的死实现（真正的注册链走 `protocol/mixins/signup.py`
的 `create_account`）。本轮清理删除了死实现，测试改为钉实际在跑的那条链路。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from platforms.chatgpt.protocol.mixins.signup import _random_birthdate


class BirthdateRangeTests(unittest.TestCase):
    def test_stays_within_adult_bounds(self):
        current_year = datetime.now(timezone.utc).year
        for _ in range(100):
            birthdate = _random_birthdate()
            year = int(birthdate.split("-", 1)[0])
            age = current_year - year
            self.assertGreaterEqual(age, 20, f"出生年份过小: {birthdate}")
            self.assertLessEqual(age, 45, f"出生年份过大: {birthdate}")

    def test_is_a_calendar_valid_iso_date(self):
        for _ in range(50):
            birthdate = _random_birthdate()
            # 格式非法或日期不存在（如 2 月 30 日）都会抛 ValueError
            parsed = datetime.strptime(birthdate, "%Y-%m-%d")
            self.assertGreaterEqual(parsed.year, 1900)


if __name__ == "__main__":
    unittest.main()
