"""部署配置契约：compose/Dockerfile 要方便在服务器上部署，且不留过时残留。

背景（全部实测过）：

- 本机 Docker Hub 被墙，``FROM node:20-bookworm-slim`` 直拉会 TLS 失败 ——
  基础镜像必须能通过 build-arg 换成可达的镜像源，否则在受限网络里根本构建不了。
  两个 ARG 还必须声明在**首个 FROM 之前**（stage 作用域；实测声明在 FROM
  之后时构建报 "base name should not be blank"）。
- ``reference/`` 有 1.1GB 参考仓库、``.secrets/`` 是旧版凭据密钥位置 ——
  不在 .dockerignore 里时每次构建都白打包/白烤进镜像。
- compose 没有 healthcheck 时 ``docker ps`` 只显示 running，进程挂死（HTTP 无
  响应）看不出来；本机还跑着 watchtower，unhealthy 状态对它有实际意义。
- 没有日志上限时 json-file 日志在长跑服务器上无限膨胀，最后撑爆磁盘。
- ``8317`` 映射是「本地插件编译 CLIProxyAPI」时代的残留：插件功能已删、
  面板全部远程化，容器里没有任何进程监听 8317。
- ``/runtime/mail`` 与 ``/runtime/plugins/*`` 已无消费者（entrypoint 的注释
  明说「留着只会在挂载卷里堆空目录」），Dockerfile 也不该在构建期预建。
- Go 工具链与 uv 只服务于已删除的本地插件编译，现在全仓零消费者。
- git 只服务于 main.py 的版本戳，但 ``.git`` 被 .dockerignore 排除，容器里
  永远读不到（实测版本恒为「未知」）—— 白付 25-35MB 镜像体积。

断言原则：能解析语义就不匹配原文。匹配原文的断言会把「解释为什么不做某事」
的注释也当成违规（8317 的说明注释就这样被逼删过），或者删了真配置、注释还在
照样绿（.dockerignore 踩过）。新增断言在改动前应当失败（RED），由部署文件
改动转绿（GREEN）。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILES = ["docker-compose.yml"]
DOCKERFILES = ["Dockerfile"]


def _text(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def _joined(name: str) -> str:
    """把 Dockerfile 的续行（反斜杠 + 换行）拼成逻辑行，便于对 RUN 内容做匹配。"""
    return _text(name).replace("\\\n", " ")


def _compose(name: str) -> dict:
    return yaml.safe_load(_text(name))


def _app_service(name: str) -> dict:
    return _compose(name)["services"]["app"]


def _effective_lines(name: str) -> list[str]:
    """去注释、去空行后的有效配置行（.dockerignore 用）。"""
    return [
        line.strip()
        for line in _text(name).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


# ── healthcheck ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_compose_declares_healthcheck(name):
    """compose 要有 healthcheck：docker ps 要能看出「服务是否真的可用」。"""
    healthcheck = _app_service(name).get("healthcheck")
    assert healthcheck, f"{name} 没有 healthcheck，容器 running ≠ 服务可用"
    test_cmd = healthcheck["test"]
    joined = " ".join(test_cmd) if isinstance(test_cmd, list) else str(test_cmd)
    # 必须探豁免鉴权的端点（/api/auth/ 前缀直接放行），设了密码也要能过
    assert "/api/auth/" in joined, (
        f"{name} 的 healthcheck 应探测豁免鉴权的 /api/auth/* 端点，"
        "否则设了登录密码后 healthcheck 永远失败"
    )
    assert healthcheck.get("start_period"), (
        f"{name} 的 healthcheck 没有 start_period —— 启动建库/加载平台需要时间，"
        "不给宽限期会把正常启动判成 unhealthy"
    )


# ── 日志轮转 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_compose_rotates_logs(name):
    """日志要有上限，否则长跑服务器上 json-file 会无限膨胀。"""
    logging_cfg = _app_service(name).get("logging") or {}
    options = logging_cfg.get("options") or {}
    assert options.get("max-size"), f"{name} 没有限制日志体积（max-size）"
    assert options.get("max-file"), f"{name} 没有限制日志文件数（max-file）"


# ── 端口可配 ────────────────────────────────────────────────────────────────


def test_compose_ports_are_configurable():
    """两个端口都要能由 .env 覆盖。

    解析 YAML 而不是匹配原文 —— 把 ports 行注释掉时原文断言照样绿，
    而 compose 已无任何端口映射（实测过的变异）。
    """
    ports = _app_service("docker-compose.yml").get("ports") or []
    joined = " ".join(str(p) for p in ports)
    assert "${APP_PORT_BIND" in joined, "8000 端口应可用 APP_PORT_BIND 覆盖"
    assert "${SOLVER_PORT_BIND" in joined, "8889 端口应可用 SOLVER_PORT_BIND 覆盖"


# ── env_file ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_compose_reads_env_file(name):
    """compose 要把 .env 注入容器（应用读 OPENAI_* 等），且缺文件不阻塞启动。"""
    env_file = _app_service(name).get("env_file")
    assert env_file, (
        f"{name} 没有 env_file —— 写进 .env 的 OPENAI_* 等应用变量进不了容器"
        "（compose 默认只用 .env 做 ${} 插值）"
    )
    entries = [env_file] if isinstance(env_file, str) else env_file
    first = entries[0]
    if isinstance(first, dict):
        assert first.get("path") == ".env", f"{name} env_file 应指向 .env"
        assert first.get("required") is False, (
            f"{name} 的 env_file 应允许缺失（required: false），"
            "否则没建 .env 的机器无法启动"
        )
    else:
        assert str(first) == ".env", f"{name} env_file 应指向 .env"


# ── APP_CONDA_ENV（容器内不许打假警告）──────────────────────────────────────


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_compose_sets_app_conda_env(name):
    """容器里不该出现「未检测到 conda 环境」假警告。"""
    environment = _app_service(name).get("environment") or {}
    assert str(environment.get("APP_CONDA_ENV", "")).strip() == "docker", (
        f"{name} 缺少 APP_CONDA_ENV=docker —— 启动会打假警告误导运维"
    )


@pytest.mark.parametrize("name", DOCKERFILES)
def test_dockerfiles_set_app_conda_env(name):
    assert "APP_CONDA_ENV=docker" in _text(name), f"{name} 缺少 APP_CONDA_ENV=docker"


# ── 过时残留 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_compose_has_no_dead_cliproxyapi_port(name):
    """8317 映射是本地插件时代的残留：容器里没有进程监听它。

    只查解析后的 ports 条目 —— 匹配原文会把「说明为什么不再映射 8317」
    的注释也判红（这个断言的第一版就这么坑过）。
    """
    offenders = [p for p in (_app_service(name).get("ports") or []) if "8317" in str(p)]
    assert not offenders, f"{name} 还有 8317 死映射（容器内无监听者）: {offenders}"


@pytest.mark.parametrize("name", DOCKERFILES)
def test_dockerfile_does_not_precreate_removed_dirs(name):
    """mail/ 与 plugins/ 已无消费者（entrypoint 注释明说），构建期也不该再预建。"""
    text = _text(name)
    assert "/runtime/mail" not in text, f"{name} 还在预建 /runtime/mail"
    assert "/runtime/plugins" not in text, f"{name} 还在预建 /runtime/plugins"


def test_full_dockerfile_drops_unused_toolchains():
    """Go 与 uv 只服务于已删除的本地插件编译，全仓已无消费者。"""
    text = _text("Dockerfile")
    assert "go.dev/dl" not in text, "Go 工具链已无消费者，不该再进镜像"
    assert "astral.sh/uv" not in text, "uv 已无消费者，不该再进镜像"


@pytest.mark.parametrize("name", DOCKERFILES)
def test_dockerfiles_do_not_install_git(name):
    """git 只服务于 main.py 的版本戳，而 .git 被 .dockerignore 排除 ——
    容器里永远读不到（实测返回「未知」），装了是白付 25-35MB 镜像体积。"""
    offenders = [
        line
        for line in _joined(name).splitlines()
        if re.search(r"apt-get install[^\n]*\bgit\b", line)
    ]
    assert not offenders, f"{name} 仍在装 git（容器内无消费者的死功能）: {offenders}"


@pytest.mark.parametrize("name", DOCKERFILES)
def test_dockerfiles_install_curl_for_healthcheck(name):
    """compose 的 healthcheck 用 curl 探测 —— 镜像里必须装它，否则永远 unhealthy。"""
    assert re.search(r"apt-get install[^\n]*\bcurl\b", _joined(name)), (
        f"{name} 未装 curl，healthcheck 会永远失败"
    )


# ── 构建上下文 ──────────────────────────────────────────────────────────────


def test_dockerignore_excludes_heavy_local_dirs():
    """reference/ 1.1GB、.hermes/、.secrets/（旧版密钥位置）
    都不该进构建上下文。

    只看有效行（去注释）—— 匹配原文时删掉真排除行、注释还在照样绿
    （该文件自己的注释里就写着「reference/ 约 1.1GB」）。
    """
    lines = _effective_lines(".dockerignore")
    for entry in ("reference/", ".hermes/", ".secrets/"):
        assert entry in lines, f".dockerignore 缺少 {entry}（构建上下文会白打包它）"


def test_dockerignore_keeps_runtime_scripts():
    """scripts/ 下有构建期（install_camoufox）与运行期（grok_turnstile_mint）
    都要用的文件，不能被瘦身排除项误伤。"""
    lines = _effective_lines(".dockerignore")
    assert "scripts/" not in lines and "scripts" not in lines, (
        "scripts/ 被排除会断掉构建（install_camoufox.py 走 COPY 进镜像）"
    )


# ── 基础镜像可覆盖（受限网络构建） ──────────────────────────────────────────


@pytest.mark.parametrize("name", DOCKERFILES)
def test_base_images_are_overridable(name):
    """受限网络（如本机 Docker Hub 被墙）要能用 --build-arg 换基础镜像源。

    只断言「两段字符串都存在」不够 —— ARG 挪到首个 FROM 之后时两个谓词
    仍为真，而构建会报 "base name should not be blank"（实测踩过）。
    这里断言行序：ARG 必须在首个 FROM 之前。
    """
    lines = _text(name).splitlines()
    first_from = next(i for i, line in enumerate(lines) if line.startswith("FROM "))
    for var in ("NODE_IMAGE", "PYTHON_IMAGE"):
        arg_lines = [
            i for i, line in enumerate(lines) if line.strip().startswith(f"ARG {var}")
        ]
        assert arg_lines, f"{name} 缺少 ARG {var}"
        assert min(arg_lines) < first_from, (
            f"{name} 的 ARG {var} 声明在首个 FROM 之后 —— ARG 是 stage 作用域，"
            "进不了 FROM 解析（构建报 'base name should not be blank'）"
        )
        assert f"FROM ${{{var}}}" in _text(name), f"{name} 的 FROM 应使用 ${{{var}}}"


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_compose_forwards_image_args(name):
    """compose 要把 NODE_IMAGE / PYTHON_IMAGE 透传给 build。"""
    build = _app_service(name).get("build") or {}
    args = build.get("args") or {}
    assert "NODE_IMAGE" in args, f"{name} 没有把 NODE_IMAGE 透传给构建"
    assert "PYTHON_IMAGE" in args, f"{name} 没有把 PYTHON_IMAGE 透传给构建"


def test_dockerfiles_do_not_require_hub_syntax_frontend():
    """syntax 指令会强制从 Docker Hub 拉 BuildKit 前端镜像，受限网络下直接卡死。

    实测：保留 `# syntax=docker/dockerfile:1.7` 时，构建第一步报
    `failed to resolve source metadata for docker.io/docker/dockerfile:1.7`
    （no route to host）。两个文件都没用到需要显式前端的特性。

    只认「行首的 # syntax=」指令本身（兼容 #syntax= / 大小写等拼写变体，
    BuildKit 对 `#` 后按 `key = value` 匹配、键名小写化）—— 说明为什么不用
    它的注释里同样会出现这个字符串（第一版断言 `not in` 被自己的注释坑成假阳性）。
    """
    pattern = re.compile(r"^\s*#\s*syntax\s*=", re.IGNORECASE)
    for name in DOCKERFILES:
        for lineno, line in enumerate(_text(name).splitlines(), 1):
            if pattern.match(line):
                raise AssertionError(
                    f"{name}:{lineno} 仍有 syntax 指令 —— 受限网络里会强制拉 "
                    "Docker Hub 前端镜像"
                )


# ── .env.example ────────────────────────────────────────────────────────────


def test_env_example_exists_and_covers_deploy_knobs():
    example = REPO_ROOT / ".env.example"
    assert example.exists(), "缺少 .env.example —— 新部署不知道有哪些可配项"
    text = _text(".env.example")
    for key in (
        "APP_PORT_BIND",
        "APP_RUNTIME_BIND",
        "CAMOUFOX_VERSION",
        "SOLVER_BROWSER_TYPE",
        "NODE_IMAGE",
        "PYTHON_IMAGE",
    ):
        assert key in text, f".env.example 缺少 {key}"


def test_env_example_covers_all_compose_interpolation_keys():
    """compose 里引用的每个 ${VAR} 都要在模板里有说明 —— 新增插值键忘写模板时红灯。"""
    example = _text(".env.example")
    referenced: set[str] = set()
    for name in COMPOSE_FILES:
        referenced |= set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)", _text(name)))
    missing = sorted(key for key in referenced if key not in example)
    assert not missing, f".env.example 缺少 compose 引用的键: {missing}"


def test_gitignore_does_not_ignore_env_example():
    """.env.* 规则会吞掉 .env.example，必须有一个生效的否定规则。

    用 git check-ignore 做语义判定 —— 把 !.env.example 挪到 .env.* 规则之前时
    原文断言照样绿，但文件依然被忽略（实测过的变异）。
    """
    result = subprocess.run(
        ["git", "check-ignore", "-q", ".env.example"],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    assert result.returncode != 0, (
        ".env.example 仍被 .gitignore 忽略（模板提交不进去）—— "
        "检查 !.env.example 否定规则的顺序"
    )
