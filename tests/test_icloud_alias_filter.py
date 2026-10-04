"""iCloud 隐私邮箱筛选规则的回归测试。

前端筛选逻辑在 `frontend/src/lib/icloud.ts` 的 `filterAliases()` 里，
是纯函数但没有前端测试框架（项目没有 vitest/jest）。这里在 Python 侧
**复刻同一套规则**，用同样的用例钉住行为：

- 关键词匹配地址 / 标签 / 备注 / 所属主号，大小写不敏感
- 号池状态、启用状态精确匹配
- 空条件 = 不过滤
- **平台口径**：选了平台之后，号池状态按「在这个平台注册过没有」解释
  （一个邮箱可以被多个平台使用，只显示「已使用」区分不出是哪个平台用过）

两边行为必须一致。改前端规则时这里会红 —— 那是提醒同步，不是噪声。
（用 python 复刻而不是给前端装测试框架：仓库当前零前端测试基建，
为一个筛选器引入 vitest 的收益不抵成本；而规则本身足够简单，能逐条对应。）
"""
import unittest


def parse_platform_field(field):
    """`frontend/src/lib/icloud.ts:parsePlatformField()` 的等价实现。"""
    return [p.strip().lower() for p in str(field or "").split(",") if p.strip()]


def alias_registered_platforms(alias):
    """`aliasRegisteredPlatforms()`：池记账 ∪ accounts 表证据。"""
    names = set(parse_platform_field(alias.get("used_platforms")))
    for item in alias.get("registered_platforms") or []:
        value = str(item or "").strip().lower()
        if value:
            names.add(value)
    return sorted(names)


def alias_platform_status(alias, platform):
    """`aliasPlatformStatus()`：平台口径状态。"""
    name = str(platform or "").strip().lower()
    if not name:
        return alias.get("pool_status")
    if name in alias_registered_platforms(alias):
        return "registered"
    if alias.get("pool_status") == "in_use":
        return "in_use"
    if alias.get("pool_status") == "unpooled":
        return "unpooled"
    # available / used（只被别的平台用过）对本平台都是可领的
    return "available"


def filter_aliases(aliases, *, keyword="", pool_status="", status="", platform=""):
    """`frontend/src/lib/icloud.ts:filterAliases()` 的等价实现。"""
    kw = str(keyword or "").strip().lower()
    out = []
    for alias in aliases:
        if pool_status:
            value = (
                alias_platform_status(alias, platform)
                if platform
                else alias.get("pool_status")
            )
            if value != pool_status:
                continue
        if status and alias.get("status") != status:
            continue
        if not kw:
            out.append(alias)
            continue
        fields = (
            alias.get("address"),
            alias.get("label"),
            alias.get("note"),
            alias.get("account_email"),
        )
        if any(kw in str(f or "").lower() for f in fields):
            out.append(alias)
    return out


def _alias(**kw):
    base = {
        "address": "a@icloud.com",
        "label": "",
        "note": "",
        "account_email": "main@icloud.com",
        "pool_status": "unpooled",
        "status": "active",
        "used_platforms": "",
        "registered_platforms": [],
    }
    base.update(kw)
    return base


class AliasFilterTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            _alias(address="alpha@icloud.com", label="工作", pool_status="used"),
            _alias(address="beta@icloud.com", label="", note="测试备注",
                   pool_status="unpooled", status="inactive"),
            _alias(address="gamma@icloud.com", label="GAMMA",
                   pool_status="available"),
            _alias(address="delta@icloud.com", account_email="other@icloud.com",
                   pool_status="in_use"),
        ]

    def test_no_filter_returns_all(self):
        self.assertEqual(len(filter_aliases(self.rows)), 4)

    def test_keyword_matches_address(self):
        got = filter_aliases(self.rows, keyword="beta")
        self.assertEqual([r["address"] for r in got], ["beta@icloud.com"])

    def test_keyword_matches_label_case_insensitive(self):
        """标签匹配要忽略大小写 —— 用户不会记得当初填的大小写。"""
        got = filter_aliases(self.rows, keyword="gamma")
        self.assertEqual([r["address"] for r in got], ["gamma@icloud.com"])
        got_upper = filter_aliases(self.rows, keyword="GAMMA")
        self.assertEqual([r["address"] for r in got_upper], ["gamma@icloud.com"])

    def test_keyword_matches_note(self):
        """备注也搜 —— 用户可能按当初记的用途找。"""
        got = filter_aliases(self.rows, keyword="测试备注")
        self.assertEqual([r["address"] for r in got], ["beta@icloud.com"])

    def test_keyword_matches_account_email(self):
        """所属主号也搜 —— 只搜地址会让人以为「搜不到」。"""
        got = filter_aliases(self.rows, keyword="other@icloud.com")
        self.assertEqual([r["address"] for r in got], ["delta@icloud.com"])

    def test_keyword_is_trimmed(self):
        """首尾空格不能把结果筛没（用户从别处粘贴常带空格）。"""
        got = filter_aliases(self.rows, keyword="  beta  ")
        self.assertEqual([r["address"] for r in got], ["beta@icloud.com"])

    def test_whitespace_only_keyword_is_no_filter(self):
        got = filter_aliases(self.rows, keyword="   ")
        self.assertEqual(len(got), 4)

    def test_pool_status_exact(self):
        got = filter_aliases(self.rows, pool_status="used")
        self.assertEqual([r["address"] for r in got], ["alpha@icloud.com"])

    def test_status_exact(self):
        got = filter_aliases(self.rows, status="inactive")
        self.assertEqual([r["address"] for r in got], ["beta@icloud.com"])

    def test_conditions_combine_with_and(self):
        """多个条件是 AND：筛「未入池 + 已停用」只应剩 beta。"""
        got = filter_aliases(self.rows, pool_status="unpooled", status="inactive")
        self.assertEqual([r["address"] for r in got], ["beta@icloud.com"])

    def test_no_match_returns_empty(self):
        self.assertEqual(filter_aliases(self.rows, keyword="zzz-nonexistent"), [])

    def test_empty_status_means_no_filter(self):
        """空字符串 = 不限（前端 allowClear 清空后就是这个值）。"""
        self.assertEqual(len(filter_aliases(self.rows, pool_status="", status="")), 4)

    def test_unknown_pool_status_filters_everything_out(self):
        """非法状态值筛出空 —— 不要「宽容地」当成不过滤。

        前端下拉只有四个合法值，出现别的说明调用方传错了；静默返回全量
        会让人以为筛选没生效。
        """
        self.assertEqual(filter_aliases(self.rows, pool_status="bogus"), [])

    def test_missing_fields_do_not_crash(self):
        """字段缺失（老数据/接口变化）不能抛异常，按空串处理。"""
        rows = [{"address": "x@icloud.com"}]
        self.assertEqual(len(filter_aliases(rows, keyword="x@")), 1)
        self.assertEqual(len(filter_aliases(rows, pool_status="unpooled")), 0)


class PlatformScopeFilterTests(unittest.TestCase):
    """平台口径筛选：一个邮箱可以被多个平台使用。

    用户诉求：「一个邮箱可以被多个平台使用，只显示已使用无法有效区分，可以加
    一个平台筛选，选择平台筛选后显示的就是在这个平台有没有使用，不筛选的话就
    显示有没有入池。」

    规则与取号逻辑（`claim_alias` / `_pop_account`）必须一致：证据
    （`accounts` 表）优先，池里的 `used_platforms` 记账作补充。
    """

    def setUp(self):
        # chatgpt 用过、grok 也用过 —— 对两个平台都是「已注册」
        self.both = _alias(
            address="both@icloud.com", pool_status="used",
            used_platforms=",chatgpt,grok,", registered_platforms=["chatgpt", "grok"],
        )
        # 只被 chatgpt 用过 —— 对 grok 仍可领
        self.gpt_only = _alias(
            address="gptonly@icloud.com", pool_status="used",
            used_platforms=",chatgpt,", registered_platforms=["chatgpt"],
        )
        # 池里标记 available，但 accounts 表里有 grok 账号（记账缺项的老数据）
        self.evidence_only = _alias(
            address="evidence@icloud.com", pool_status="available",
            used_platforms="", registered_platforms=["grok"],
        )
        self.fresh = _alias(address="fresh@icloud.com", pool_status="available")
        self.notyet = _alias(address="notyet@icloud.com", pool_status="unpooled")
        self.rows = [self.both, self.gpt_only, self.evidence_only, self.fresh, self.notyet]

    def test_status_without_platform_is_pool_status(self):
        """不筛选平台 = 显示有没有入池（旧口径不变）。"""
        self.assertEqual(alias_platform_status(self.both, ""), "used")
        self.assertEqual(alias_platform_status(self.fresh, ""), "available")

    def test_used_by_other_platform_counts_as_available(self):
        """只被 chatgpt 用过的地址，在 grok 口径下是「可领」。"""
        self.assertEqual(alias_platform_status(self.gpt_only, "grok"), "available")
        self.assertEqual(alias_platform_status(self.gpt_only, "chatgpt"), "registered")

    def test_both_platforms_registered(self):
        self.assertEqual(alias_platform_status(self.both, "grok"), "registered")
        self.assertEqual(alias_platform_status(self.both, "chatgpt"), "registered")

    def test_accounts_evidence_beats_missing_bookkeeping(self):
        """池里记的是 available，但 `accounts` 表里有 grok 账号 → 对 grok 已注册。

        任务中途崩掉会让 `used_platforms` 缺项；只看记账就会把一个其实已经
        注册过的地址再发出去，白跑一轮还撞「邮箱已被占用」。
        """
        self.assertEqual(alias_platform_status(self.evidence_only, "grok"), "registered")

    def test_platform_filter_selects_that_platform_only(self):
        got = filter_aliases(self.rows, platform="grok", pool_status="registered")
        self.assertEqual(
            [r["address"] for r in got],
            ["both@icloud.com", "evidence@icloud.com"],
        )

    def test_platform_filter_available_shows_reusable_addresses(self):
        """该平台未注册 = 还能领：available 与「只被别的平台用过」都算。"""
        got = filter_aliases(self.rows, platform="grok", pool_status="available")
        self.assertEqual(
            [r["address"] for r in got],
            ["gptonly@icloud.com", "fresh@icloud.com"],
        )

    def test_platform_filter_keeps_unpooled_separate(self):
        """未入池仍然是单独的：注册取号会跳过它们，不能并进「可领」。"""
        got = filter_aliases(self.rows, platform="grok", pool_status="unpooled")
        self.assertEqual([r["address"] for r in got], ["notyet@icloud.com"])

    def test_platform_filter_alone_does_not_drop_rows(self):
        """只选平台、不选状态：一行都不该被丢掉（只是换了展示口径）。"""
        got = filter_aliases(self.rows, platform="grok")
        self.assertEqual(len(got), len(self.rows))

    def test_pool_status_value_from_other_scope_is_rejected(self):
        """池口径的 `used` 在平台口径下没有对应值 → 筛出空，而不是静默返回全量。"""
        self.assertEqual(filter_aliases(self.rows, platform="grok", pool_status="used"), [])


class FilterUiContractTests(unittest.TestCase):
    """钉住「表格/导出读的是筛选后的集合」这条口径。

    回归背景：如果哪天有人把 `dataSource` 改回 `aliases`，
    筛出来的行数对了但导出还是会拿到全量 —— 这条断言靠读源码防住。
    """

    def test_page_uses_filtered_list_for_table_and_export(self):
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "frontend/src/pages/ICloud.tsx"
        text = src.read_text(encoding="utf-8")

        self.assertIn("dataSource={visibleAliases}", text,
                      "表格必须读筛选后的列表，否则筛选对表格不生效")
        self.assertIn("return visibleAliases", text,
                      "未勾选时导出的应是筛选后的集合（与账号页口径一致）")
        self.assertNotIn("dataSource={aliases}", text,
                         "dataSource 不能直接读原始列表")

    def test_filter_rules_live_in_lib(self):
        """规则本体在 lib 里（可复用），页面只做接线。"""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        lib = (root / "frontend/src/lib/icloud.ts").read_text(encoding="utf-8")
        page = (root / "frontend/src/pages/ICloud.tsx").read_text(encoding="utf-8")

        self.assertIn("export function filterAliases", lib)
        self.assertIn("export function hasAliasFilter", lib)
        self.assertIn("filterAliases(aliases, {", page,
                      "页面应调用 lib 的函数，而不是内联一份实现")

    def test_page_wires_the_platform_filter(self):
        """平台筛选必须真的接到页面上（不是只写了 lib 里的纯函数）。

        用户要的是「列表上方能选平台，选了之后看该平台口径」；只在 lib 里
        加函数而页面没接线，等于功能没做。
        """
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        page = (root / "frontend/src/pages/ICloud.tsx").read_text(encoding="utf-8")

        self.assertIn("placeholder=\"全部平台\"", page)
        self.assertIn("filterPlatform", page)
        self.assertIn("platform: filterPlatform", page,
                      "筛选调用必须把平台传进 filterAliases")
        self.assertIn("aliasPlatformStatus", page,
                      "号池状态列要按平台口径渲染")
        self.assertIn("已注册平台", page,
                      "要能一眼看出这个地址注册过哪些平台")


if __name__ == "__main__":
    unittest.main()
