# 全量检测 · 2026-11-15

**目标**：整个项目全量检测 —— 每条流程是否**正确、已优化、可用**。
**方式**：17 条只读检测 lane 并行（2 波）+ 控制器侧复现门禁 + 确认后 TDD 修复。
**基线**：`HEAD fca2eec`，工作树 **dirty**（82 files, +5366/−1650）。检测对象是**磁盘上的工作树**，不是 HEAD。

## 预检基线（任何 lane 动手之前记录）

| 项 | 值 | 命令 |
|---|---|---|
| HEAD | `fca2eec` | `git rev-parse --short HEAD` |
| 工作树 | 118 条 status，82 文件改动，+5366/−1650 | `git status --short \| wc -l` / `git diff --stat` |
| 跟踪文件 | 715 | `git ls-files \| wc -l` |
| Python | 3.12.3（`.venv/bin/python`），pytest 9.1.1 | `.venv/bin/python -V` |
| 前端 | React 19 + antd 5 + Vite 8，`node_modules` 已装（175 项），30 个自检脚本 | `ls frontend/node_modules \| wc -l` |
| `tsc -b` | exit 0（基线全绿） | `cd frontend && npx tsc -b` |
| ruff | 未安装（`.venv` 内无 ruff 模块）→ lint 门禁实际不存在 | `.venv/bin/python -m ruff check .` |
| 全量 pytest | 控制器单跑，见 `pytest-baseline.txt` | `.venv/bin/python -m pytest tests/ -q` |

## 归属表

每个跟踪文件**恰好**归属一条 lane（0 未归属、0 重复），机器可读版本见 `inventory.json`。

| lane | 领地 | 文件数 |
|---|---|---|
| L01-auth-config-backup | main.py / auth / config / backup / 免登录邮件页 / net_utils | 10 |
| L02-accounts-storage | 账号 CRUD / 存储后端 / 加密 / 导入导出 | 15 |
| L03-tasks-runtime | 任务生命周期 / 运行时 / 调度 / 动作执行 / 日志 | 10 |
| L04-proxies | 代理池 / URL 解析 / 打码 / 分配与换绑 / tools | 11 |
| L05-icloud | iCloud 别名池 / HME / share_token / 远端 API | 28 |
| L06-mail | 邮箱通道 / Outlook 后端 / 邮件导入 / OTP | 22 |
| L07-panels | 面板对比/同步/推送/登记 / contribution | 14 |
| L08-frontend | React 面板 / 30 个自检脚本 / static | 101 |
| L09-gates | 门禁自身（测试/类型/lint/脚本/CI） | — |
| L10-data-migrations | 迁移链 / 引擎注册 / merge / paths | 15 |
| L11-platform-chatgpt | ChatGPT 协议链 + chatgpt_* 服务 | 41 |
| L12-platform-grok-kiro-aws | Grok / Kiro / AWS 平台 + 注册表 | 34 |
| L13-services-misc | solver / SMS / 验证码 / 维护任务 | 20 |
| L14-docs-deploy | docs / Docker / compose / requirements / CI | 75 |
| L15-tests-deadcode | tests 质量 + 死面（无调用方符号/无读方配置键） | 289 |
| L16-core-runtime | core 基础设施（executors/browser/console/模型） | 20 |
| L17-scripts | scripts/ 运维与检查脚本 | 10 |

## 每条 lane 的硬约束

1. **只读**：除自己的报告文件外不改任何文件；不 git add/commit/checkout/stash。
2. 不重启服务、不占端口、不起后台进程、不访问第三方。
3. **不跑全量 `pytest tests/`**（控制器独占该门禁）；只跑自己领地相关的测试文件。
4. 每条 finding 必须带 `file:line` + 实际执行的命令与输出片段 + 一行触发条件；未证实的假设进 `checked_and_clean`。
5. 报告先写空骨架再逐条追加，被杀也留得下合法 JSON。
6. **按面（surface）而非按列**清点凭据外泄：列 / extra blob / 日志 / API 响应 / CSV / 备份包，逐个给出出口函数。

## 严重度

- **P0** 数据丢失 / 凭据泄漏 / 正常用户走不通的流程
- **P1** 常见路径给出错误结果，或某道控制实际不成立
- **P2** 边界情况、性能、有限爆炸半径的漂移
- **P3** 装饰性 / 死代码

## 阶段

- [x] 预检基线
- [x] 归属表（inventory.json）
- [ ] 波次 1：L01–L10
- [ ] 波次 2：L11–L17
- [ ] 复现门禁：控制器逐条复现 P0/P1（`docs/audit/2026-11-15-full/repro.md`）
- [ ] 确认后修复（TDD，先失败测试）
- [ ] 终局门禁：全量 pytest + `tsc -b` + `npm run check`
- [ ] 汇总报告：**检测完成**与**修复完成**分两行陈述，不合并

## 产物

| 文件 | 内容 |
|---|---|
| `inventory.json` | lane → 文件清单（含 `_meta`） |
| `L01..L17-*.json` | 各 lane 原始结论 |
| `aggregate.py` / `aggregate.json` | 汇总（容忍字段拼写差异，校验未覆盖文件） |
| `pytest-baseline.txt` | 控制器全量测试基线 |
| `repro.md` | 逐条复现记录（含伪证 falsified） |
| `SUMMARY.md` | 最终报告 |
