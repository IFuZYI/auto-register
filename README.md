# 账号管理台

多平台账号自动注册与管理系统：插件化平台层、Web 管理台、批量注册、邮箱号池、
面板对比上传、数据一键迁移，并随后端自动拉起本地 Turnstile Solver。

- **平台**：ChatGPT（纯协议，无需浏览器）、Grok（camoufox 浏览器路径）
- **邮箱来源**：iCloud 隐私邮箱（本地）、Outlook / Hotmail（本地号池）
- **面板对接**：CPA（CLIProxyAPI）、Sub2API、grok2api、chatgpt2api

> ⚠️ 免责声明：本项目仅供学习与研究使用，不得用于任何商业用途。使用本项目所产生的一切后果由使用者自行承担。

## 目录

- [核心功能](#核心功能)
- [部署方式](#部署方式)
- [使用教程](#使用教程)
- [界面导览](#界面导览)
- [常见问题排查](#常见问题排查)
- [项目结构](#项目结构)
- [文档索引](#文档索引)
- [界面预览](#界面预览)

## 核心功能

- **多平台账号注册与管理**：统一的账号列表、详情、导入（文本 / JSON 全字段）、多格式导出、删除、批量操作
- **批量注册**：注册数量、并发数、每个账号启动延迟、失败重试轮数；支持邮箱 / 手机 / 手机 + 邮箱三种流程
- **邮箱号池**：iCloud 隐私邮箱（本地）与 Outlook / Hotmail（本地号池），邮箱按平台消耗
- **Token 生命周期**：刷新 Token、补 RT、绑定 TOTP 2FA、探测状态、检测 Plus 试用，全部跑成可停止的后台任务
- **账号状态**：正常 / 过期 / 失效 / 禁用四档，封禁靠登录链发掘（详见[功能详解](docs/FEATURES.md)）
- **代理能力**：代理池轮询与健康检查；账号保留注册代理，复用时优先走同一个出口
- **面板对比**：本地 ↔ 远程账号逐项比对（凭证是否一致），并在对比页直接上传 / 同步
- **验证码支持**：YesCaptcha、本地 Turnstile Solver（Camoufox）、手动
- **手机接码**：SmsBower / HeroSMS 自动租号收码，ChatGPT 命中 add-phone 时全程无人值守
- **实时日志**：注册任务与批量任务的前端实时日志流（SSE），支持停止 / 跳过
- **数据迁移**：数据与配置一键导出 / 导入（服务迁移用）

平台专项能力（ChatGPT 注册协议与 Token 方案、Grok 浏览器路径、面板对接细节、
导出格式清单）见 [功能详解](docs/FEATURES.md)。

## 部署方式

提供四种部署方式，按场景选择：

| 方式 | 适合场景 | 需要 |
| --- | --- | --- |
| [Docker Compose（推荐）](#方式一docker-compose推荐) | 服务器长期运行、要持久化与自动重启 | Docker + Compose |
| [Docker 单容器](#方式二docker-单容器) | 已有编排（1Panel / Portainer / 宝塔），或想手动控制参数 | Docker |
| [裸机部署](#方式三裸机部署) | 本机开发、无 Docker 环境、需要改代码即跑 | Python 3.12+ / Node |
| [开发模式](#方式四开发模式前端热更新) | 改前端代码要热更新 | 上述 + Node 18+ |

> 无论哪种方式，**对外暴露前先设登录密码**（见[使用教程](#1-设置登录密码对外暴露前必做)）。
> 完整环境变量、数据目录、反向代理要点见 [部署与配置](docs/DEPLOYMENT.md)。

### 方式一：Docker Compose（推荐）

```bash
git clone https://github.com/IFuZYI/auto-register.git Register && cd Register
cp .env.example .env              # 可选：按需改端口 / 数据目录 / 镜像源
docker compose up -d --build      # 首次构建较慢（下载依赖 + 浏览器）
docker compose logs -f app        # 看启动日志
```

启动后访问 <http://localhost:8000>。常用管理命令：

```bash
docker compose ps                 # 状态（含 healthcheck）
docker compose restart            # 重启
docker compose down               # 停止（数据保留在 ./data）
docker compose up -d --build      # 更新代码后重建
```

要点：

- **数据持久化**：所有运行数据（库 / 密钥 / 日志）在宿主机的 `./data`（由
  `APP_RUNTIME_BIND` 可改）—— 备份/迁移就是拷这一个目录，或直接用界面里的
  「全局配置 → 数据迁移」。
- **端口**：`APP_PORT_BIND`（默认 `8000`）与 `SOLVER_PORT_BIND`（默认只绑本机
  `127.0.0.1:8889`）可在 `.env` 里改，例 `APP_PORT_BIND=127.0.0.1:8000`
  只绑本机交给反向代理。
- **受限网络**：Docker Hub 不可达时在 `.env` 里把 `NODE_IMAGE` / `PYTHON_IMAGE`
  换成可达镜像源再构建（模板里有示例）。
- 面板（CPA / Sub2API / grok2api / chatgpt2api）需**自行部署在别处**，
  本应用不安装、不启动它们。

### 方式二：Docker 单容器

不用 Compose、由其他编排工具管理时，直接 `docker run`：

```bash
# 1) 构建（在仓库根执行）
docker build -t account-manager:latest .

# 2) 运行
docker run -d \
  --name account-manager \
  --init \
  --restart unless-stopped \
  -p 8000:8000 \
  -v "$(pwd)/data:/runtime" \
  -e DATA_DIR=/runtime \
  -e CREDENTIAL_ENCRYPTION_KEY_FILE=/runtime/secrets/credential_key \
  -e APP_ENABLE_SOLVER=1 \
  -e SOLVER_BIND_HOST=0.0.0.0 \
  -e LOCAL_SOLVER_URL=http://127.0.0.1:8889 \
  account-manager:latest
```

- **`--init`**：容器 PID 1 需要 init 转发信号 —— 镜像 ENTRYPOINT 已用 `tini`
  包裹，`--init` 只是双保险（compose 对应 `init: true`）。
- **`-v ...:/runtime` 必须加**：不挂卷则每次重建容器数据全丢，且凭据加密密钥
  重新生成会让库里已加密的凭据全部解不开。
- 需要应用变量（`OPENAI_*` 等）时加 `--env-file .env`（文件格式与 compose 通用）。

### 方式三：裸机部署

#### 1. 创建并激活 Python 环境

```bash
python -m venv .venv && . .venv/bin/activate
# 或 conda create -n account-manager python=3.12 -y && conda activate account-manager
```

#### 2. 安装后端依赖

```bash
pip install -r requirements.txt
```

#### 3. 安装浏览器相关依赖

```bash
python -m playwright install chromium
python -m camoufox fetch
```

> 升级过 camoufox 包（pip 装新版）之后**必须重跑一次 `python -m camoufox fetch`** ——
> 每个 camoufox 包版本钉死配套的浏览器 build。

#### 4. 安装并构建前端

```bash
cd frontend
npm install
npm run build
cd ..
```

构建完成后，静态资源输出到 `./static`。

#### 5. 启动

```bash
python main.py
```

启动后默认访问 <http://localhost:8000>（前端由 FastAPI 直接托管，不是 `5173`）。

后台长跑建议用进程管理器或 systemd 单元（手工 `nohup` 也可以，但**不要**用
`pkill -f main.py` 停服 —— 参考 [维护手册](docs/MAINTENANCE.md) 的重启约定）：

```bash
nohup python main.py > data/logs/server.log 2>&1 &
```

### 方式四：开发模式（前端热更新）

终端 1 启动后端（`python main.py`），终端 2：

```bash
cd frontend
npm run dev
```

访问 <http://localhost:5173>。Vite 会把 `/api` 请求代理到本地后端 `http://localhost:8000`。

Docker 部署细节、环境变量清单与 Turnstile Solver 说明见 [部署与配置](docs/DEPLOYMENT.md)。


## 使用教程

### 1. 设置登录密码（对外暴露前必做）

「全局配置 → 安全」设置密码。没设密码时鉴权中间件会放行所有 `/api/` 请求 ——
这个面板管着账号、Token、接码 API Key、邮箱和代理凭据。

### 2. 配置邮箱来源

在「邮箱服务」里维护收件来源，注册时从号池取地址：

- **iCloud 隐私邮箱（本地）**：添加 Apple ID 主号（账号密码或 Cookie 导入），再批量生成隐私邮箱别名
- **Outlook（本地）**：导入微软号池（Outlook / Hotmail / MailAPI URL 三类）

> ⚠️ **粘贴邮箱只是存进号池，不会自动可用。** 在预览表里勾选要用的邮箱、点
> 「导入邮箱池」（状态从「未入池」转为「可领」）之后，注册任务才能取到它们。

### 3. 配置代理（可选）

「代理管理」里增删代理、跑健康检查。账号会**保留注册代理**，复用时优先走同一个出口；
无绑定或绑定失效时会在后续操作中自动补上。

### 4. 注册账号

「平台管理 → 选平台 → 注册」打开注册弹窗（或直连 `/register` 使用完整表单）：

1. 选平台与注册方式（ChatGPT 可切「有 RT / 无 RT」、绑定 2FA、注册流程）
2. 填数量、并发数、每个账号延迟、失败重试轮数
3. 提交后弹出实时日志窗口，可随时停止 / 跳过当前账号

### 5. 账号管理与批量操作

账号列表支持搜索、状态 / Plus 试用 / 时间筛选、双击查看详情。操作菜单（单账号）
与「更多」菜单（批量，按「所选」或「当前筛选范围」）包括：

- **刷新 Token**：session → OAuth → 登录链拿 AT，真校验后才写库；只返还原 AT（未换发）会走登录流程换发，如实区分「已换发 / 无需换发」
- **补 RT**：会话复用换 `refresh_token`，失效再走登录链
- **绑定 2FA**：绑定后密钥可在列表标签、操作菜单、详情页三处复制
- **探测本地状态 / 检测 Plus 试用**：写回状态与订阅结论
- **导出 / 导入**：文本或 JSON 全字段

### 6. 上传到面板

在「面板管理」里配置面板地址后，用**本地 ↔ 远程对比**查看哪些号没上传、
凭证是否一致，并在对比页直接上传 / 同步（方向严格分开，绝不拿旧凭证覆盖新的）。

### 7. 备份与迁移

「全局配置 → 数据迁移」把数据库与凭据密钥打包成 ZIP，在另一台机器导入即完成迁移。
细节见 [部署与配置](docs/DEPLOYMENT.md#数据目录与一键迁移)。

## 界面导览

侧栏一级菜单（共 8 项），二级项由后端接口下发，注册表加一项前端自动多一项：

| 一级菜单 | 二级 | 用途 |
| --- | --- | --- |
| **仪表盘** | — | 账号总览与分布（总账号数 / 正常 / 失效（含过期）/ 禁用四卡） |
| **任务运行** | — | 进行中与已完成的任务，实时日志流 |
| **平台管理** | ChatGPT / Grok | 按平台的账号列表、详情、导入导出、批量操作、注册弹窗 |
| **邮箱服务** | iCloud 隐私邮箱（本地）/ Outlook（本地） | 各邮箱来源的号池维护 |
| **面板管理** | CPA 面板 / Sub2API / grok2api / chatgpt2api | 面板入口卡片 + 本地 ↔ 远程账号对比 |
| **代理管理** | — | 代理池增删与健康检查 |
| **任务历史** | — | 注册任务执行记录，支持批量删除 |
| **全局配置** | 注册设置 / 验证码 / 手机接码 / 邮箱 / 面板配置 / 安全 / 数据迁移 | 全局默认值与集成配置 |

**日常注册**从「平台管理 → 注册」按钮走弹窗即可；`/register` 是一个独立的注册任务页
（可直连 URL 打开，表单更完整），不是侧栏入口。

> 旧路径 `/platform-config`、`/icloud` 等做了重定向，老书签不会 404。

## 常见问题排查

### 1. 前端里 Turnstile Solver 显示「未运行」

先检查后端是否正常启动：

```bash
curl http://localhost:8000/api/solver/status
```

正常返回 `{"running":true}`。若 `8000` 端口都访问不到，问题在后端而不是 Solver 本身。

### 2. 如何确认当前 Python 是否正确

```bash
python -c "import sys; print(sys.executable)"
```

输出的解释器应当就是安装依赖时用的那个；不是就说明环境没激活对。

### 3. 端口被占用

后端启动报 `address already in use` 时，先找到占用端口的进程停掉，再重新启动：

```bash
ss -tlnp | grep :8000      # Linux
netstat -ano | findstr :8000   # Windows
```

### 4. ChatGPT 注册收不到验证码

先确认 `node` 可执行（`node --version`）。Sentinel PoW 求解器要在 Node 沙箱里跑 OpenAI 的
`sdk.js`，没有 Node 时算出来的 token 过不了服务端复核，**验证码邮件会被静默丢弃** ——
日志上看不到明显报错，但码永远收不到。若 `node` 不在 `PATH` 里，用
`OPENAI_SENTINEL_NODE_PATH` 指定绝对路径。

### 5. 注册报「池里没有可用账号」

邮箱导入后**还要勾选入池**。粘贴只是把它们存进号池（状态「未入池」），
在预览表里勾选要用的邮箱、点「导入邮箱池」之后才可被领取。

### 6. Grok 注册发码失败

Grok 只有 camoufox 浏览器路径。确认 Solver/browser 依赖已装
（`python -m camoufox fetch`），并检查发码节流（`grok_send_code_min_interval`）——
x.ai 对同一 IP 的取码有频率限制，太密会把出口打进长冷却。

> **升级过 camoufox 包（pip 装新版）之后必须重跑一次 `python -m camoufox fetch`**：
> 每个 camoufox 包版本钉死配套的浏览器 build，包升级后旧浏览器会立刻不被认。
> 注册任务现在会**在分配邮箱之前**做环境预检，直接以这条修复指引失败，
> 不会再让每个账号白跑一轮、重试轮全烧在同一个环境错误上。

## 项目结构

```text
Register/
├── api/                  FastAPI 路由层（config / accounts / tasks / actions / backup / auth…）
├── core/                 抽象基类、注册表、任务运行时、DB 层、代理、执行器、邮箱池
├── platforms/            平台插件（chatgpt / grok）+ 邮箱客户端（icloud）
├── services/             跨平台服务：邮箱池、面板注册表与对比、外部同步、数据迁移
├── modules/              面向复用的轻量业务模块层
├── frontend/             React + TypeScript + Vite 管理台（构建产物进 static/）
├── scripts/              运行时脚本（camoufox 安装、Turnstile 铸造等）
├── tests/                pytest 测试
├── docs/                 主题专文（功能、部署、接口、扩展、维护、分库、目录规范）
├── data/                 运行时数据（见部署文档，不进版本控制）
├── docker/               容器启动脚本
├── main.py               应用入口
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

技术栈：后端 FastAPI + SQLite（SQLModel）；前端 React + TypeScript + Vite；
HTTP 用 curl_cffi；浏览器自动化用 Playwright / Camoufox；ChatGPT 注册协议纯协议实现，
Sentinel PoW 走 Node 沙箱。

## 文档索引

| 文档 | 回答的问题 |
| --- | --- |
| [docs/FEATURES.md](docs/FEATURES.md) | 平台能力、邮箱服务、面板对接、导出格式的完整说明 |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Docker Compose / 单容器部署、环境变量、数据目录与一键迁移 |
| [docs/MAINTENANCE.md](docs/MAINTENANCE.md) | 改完代码要同步动哪些文件、升级步骤、验证门禁 |
| [docs/EXTENDING.md](docs/EXTENDING.md) | 新增平台 / 注册流程 / 邮箱渠道怎么写 |
| [docs/API_REFERENCE.md](docs/API_REFERENCE.md) | 每个接口的请求 / 响应形状 |
| [docs/DATABASE_MODULARITY.md](docs/DATABASE_MODULARITY.md) | 分库、邮箱唯一键、跨库查询 |
| [docs/DATA_DIRECTORY.md](docs/DATA_DIRECTORY.md) | 数据目录布局与路径规则 |

## 界面预览

### 仪表盘

![仪表盘](docs/images/dashboard.png)

### 注册任务

![注册任务](docs/images/register-task.png)

### 全局配置

![全局配置](docs/images/settings.png)
