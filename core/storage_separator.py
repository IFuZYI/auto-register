"""导出/导入共用的分隔符。

单独成模块的原因：`services/account_export.py` 与 `api/accounts.py` 都要用它，
而后者 import 前者会拉进导出依赖链。一个常量不值得制造这条依赖。
"""

#: 与市面上的号商格式一致
SEPARATOR = "----"

__all__ = ["SEPARATOR"]
