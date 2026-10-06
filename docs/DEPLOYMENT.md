# 部署与配置

本文覆盖 Docker 部署、环境变量与数据持久化。本地直跑（venv + npm build）看
[README 的快速开始](../README.md#快速开始)；数据目录规范看
[DATA_DIRECTORY.md](DATA_DIRECTORY.md)；升级/迁移步骤看 [MAINTENANCE.md](MAINTENANCE.md)。

## Docker 部署

```bash
cp .env.example .env              # 可选：按需改端口 / 数据目录 / 镜像源
docker compose up -d --build      # 启动
docker compose down               # 停止
docker compose logs -f app        # 日志
```

首次构建会额外下载 Python 依赖、Playwright Chromium 和 Camoufox，耗时明显更长。
构建脚本通过固定直链安装 Camoufox（避免构建时访问 GitHub Releases API 触发匿名限流），
**浏览器版本自动跟随 pip 解析到的 camoufox 包**（读包自带的 `browser-pin.json`），
不会出现包与浏览器错配。uBlock Origin 附加组件为可选增强：优先从 AMO 下载，
失败自动回退 GitHub 官方签名版，两者都不可达时跳过（不影响构建）。

**数据持久化**：整站运行数据都在一个目录下（见 [数据目录](DATA_DIRECTORY.md)），
宿主机只需挂载 `./data` 这一个卷。容器内数据根由 `DATA_DIR=/runtime` 指定，
`DATABASE_URL` 默认派生为 `sqlite:////runtime/account_manager.db`。

### 部署要点

- **端口可配**：`APP_PORT_BIND`（面板）、`SOLVER_PORT_BIND`（Solver）
  可在 `.env` 里改绑定地址，例 `127.0.0.1:8000` 只绑本机交给反代。
- **基础镜像可换**：Docker Hub 不可达时（TLS 失败 / 连接重置），在 `.env` 里设
  `NODE_IMAGE` / `PYTHON_IMAGE` 为可达的镜像源再构建，无需改 Dockerfile。
- **健康检查**：compose 带 `healthcheck`（探 `/api/auth/status`，豁免登录），
  `docker ps` 直接看得到 `healthy / unhealthy`。
- **日志上限**：`json-file` 日志已限制 `10m × 3`，长跑不会撑爆磁盘。
- **`.env` 注入**：写进 `.env` 的应用变量（`OPENAI_*` 等）会注入容器；
  `environment` 段里的键（`DATA_DIR` 等）以 compose 文件为准。

> ⚠️ **对外暴露前必须先设登录密码。** 没设密码时鉴权中间件会直接放行所有 `/api/` 请求。
> 这个面板管着账号、Token、接码 API Key、邮箱和代理凭据。先在「全局配置 → 安全」里
> 设密码，或：
>
> ```bash
> curl -X POST http://127.0.0.1:8000/api/auth/setup \
>      -H 'Content-Type: application/json' -d '{"password":"你的强密码"}'
> ```
>
> 设完确认无 token 会被挡：`curl -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/config`（期望 401）。

任务日志走 SSE，nginx 反代时 `/api/tasks/` 必须关掉响应缓冲（`proxy_buffering off`），
否则前端看不到实时日志。

### Docker 使用建议

- 镜像主要覆盖主应用和本地 Turnstile Solver
- 面板（CPA / Sub2API / grok2api / chatgpt2api）需**自行部署在别处**，本应用不安装、不启动它们

## 环境变量

| 变量名 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` | `0.0.0.0` | FastAPI 监听地址 |
| `PORT` | `8000` | FastAPI 监听端口 |
| `APP_RELOAD` | `0` | 设 `1` 时启用 uvicorn 热重载（开发用） |
| `DATA_DIR` | `<仓库根>/data`（容器内 `/runtime`） | 数据根目录 |
| `DATABASE_URL` | `sqlite:///$DATA_DIR/account_manager.db` | 默认库地址 |
| `DATABASE_URL_<PLATFORM>` | — | 平台分库地址（按平台名大写） |
| `PLATFORM_DATABASE_URLS` | — | 平台分库的 JSON 总表 |
| `CREDENTIAL_ENCRYPTION_KEY` | — | 凭据加密密钥本体（base64/hex），优先于密钥文件 |
| `CREDENTIAL_ENCRYPTION_KEY_FILE` | `$DATA_DIR/secrets/credential_key` | 凭据加密密钥文件 |
| `APP_ENABLE_SOLVER` | `1` | 是否自动启动本地 Solver，设 `0` 禁用 |
| `SOLVER_PORT` | `8889` | Solver 监听端口 |
| `SOLVER_BIND_HOST` | `0.0.0.0` | Solver 绑定地址 |
| `SOLVER_BROWSER_TYPE` | `camoufox` | Solver 用的浏览器类型 |
| `SOLVER_PROXY_ENABLED` | `1` | Solver 是否启用代理支持 |
| `LOCAL_SOLVER_URL` | `http://127.0.0.1:8889` | 后端访问 Solver 的地址 |
| `OPENAI_SENTINEL_NODE_PATH` | `node` | Sentinel PoW 求解器用的 Node 可执行文件（不在 `PATH` 时填绝对路径） |
| `APP_JWT_SECRET` | 自动生成并存库 | 面板登录态 JWT 签名密钥 |

如需传入 `OPENAI_*` 等配置：Docker 部署时写进仓库根的 `.env` 即可 —— compose 已带
`env_file: .env`（`required: false`，没建该文件也能启动），应用变量会注入容器。
本机直跑时在 shell 中 `export`（`.env` 仅被配置存储作为缺省值读取）。
`environment` 段里已写死的键（`DATA_DIR` 等）以 compose 文件为准。

## 数据目录与一键迁移

运行时产生的所有数据都在仓库根的 **`data/`** 下。代码侧唯一真相源是
[`core/paths.py`](../core/paths.py)。完整规范见 [docs/DATA_DIRECTORY.md](DATA_DIRECTORY.md)。

```text
data/
├── account_manager.db        默认库：任务、任务日志、代理、全局配置   ← 必须备份
├── platforms/                平台分库（账号随平台分库）              ← 必须备份
│   └── <platform>.db
├── secrets/
│   ├── credential_key        凭据加密密钥（AES-256-GCM）             ← 必须备份
│   └── totp_journal/         2FA 绑定的写前密钥日志（可丢弃）
├── import_backups/           导入前自动留存的数据快照（可丢弃）
└── logs/                     应用自身日志（可丢弃）
```

> **一键迁移**：界面「全局配置 → 数据迁移」可把上面**必须备份**的三项打成一个 ZIP
> 导出，在另一台机器导入即完成迁移 —— 不用手工搬文件。
> 实现见 `services/data_bundle.py`，接口见 [docs/API_REFERENCE.md](API_REFERENCE.md)
> 的 `/api/backup/*`。**日志与 `import_backups/` 刻意不进包**；导入前会自动备份到
> `data/import_backups/<时间戳>-<随机后缀>-import/`，导错了可以回退。

导出用的是 SQLite 的 `VACUUM INTO`（一致性快照），不是直接拷运行中的库文件 ——
WAL/journal 活跃时直接拷会拷到半个事务。

## Turnstile Solver 说明

本地 Turnstile Solver 会在 FastAPI 后端启动时自动拉起，默认 <http://localhost:8889>。
前端「全局配置 → 验证码 → Turnstile Solver」显示的是**后端检测结果**，所以：

- 后端未启动 → 前端显示「未运行」
- 后端已启动但缺依赖 → Solver 可能启动失败（日志：`data/logs/solver.log`）

手动启动：

```bash
python services/turnstile_solver/start.py --browser_type camoufox --port 8889
```
