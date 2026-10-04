"""守住"凭据密钥必须放在挂载卷里"这条部署约束。

core.secret_box 的默认密钥路径现在是 ``data/secrets/credential_key``
（见 core/paths.py）。容器里若 ``DATA_DIR`` 没指到挂载卷，密钥就落在镜像层
而不是卷里 —— ``docker compose up --build`` 重建容器时会生成一把新密钥，
而数据库还在卷里躺着，里面所有 credentials_cipher 都变成解不开的密文
（decrypt 抛 InvalidTag），且无法恢复，用户只能重新登录所有账号。

所以两个镜像和两份 compose 都必须显式把密钥指到 /runtime 下面。
"""

from __future__ import annotations

import base64
import os
import re
from pathlib import Path

import pytest
import yaml
from cryptography.exceptions import InvalidTag

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_MOUNT = "/runtime"
ENV_VAR = "CREDENTIAL_ENCRYPTION_KEY_FILE"

DOCKERFILES = ["Dockerfile"]
COMPOSE_FILES = ["docker-compose.yml"]
ENTRYPOINTS = ["docker/entrypoint.sh"]


@pytest.mark.parametrize("name", DOCKERFILES)
def test_dockerfile_pins_key_onto_runtime_volume(name):
    text = (REPO_ROOT / name).read_text(encoding="utf-8")
    match = re.search(rf"{ENV_VAR}=(\S+)", text)
    assert match, f"{name} 没有设置 {ENV_VAR}，重建容器会换掉凭据密钥"
    assert match.group(1).startswith(RUNTIME_MOUNT + "/"), (
        f"{name} 的 {ENV_VAR}={match.group(1)} 不在 {RUNTIME_MOUNT} 挂载卷里"
    )


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_compose_pins_key_onto_runtime_volume(name):
    config = yaml.safe_load((REPO_ROOT / name).read_text(encoding="utf-8"))
    environment = config["services"]["app"]["environment"]
    assert ENV_VAR in environment, f"{name} 没有设置 {ENV_VAR}"
    assert str(environment[ENV_VAR]).startswith(RUNTIME_MOUNT + "/")


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_compose_mounts_the_runtime_volume(name):
    """密钥指向 /runtime 只有在 /runtime 真的是挂载卷时才有意义。"""
    config = yaml.safe_load((REPO_ROOT / name).read_text(encoding="utf-8"))
    volumes = config["services"]["app"]["volumes"]
    # 宿主机一侧可能写成 ${APP_RUNTIME_BIND:-./data}，冒号不能当成字段分隔符用，
    # 所以这里只检查 /runtime 作为某一段出现过。
    assert any(RUNTIME_MOUNT in str(v).split(":") for v in volumes), (
        f"{name} 没有把 {RUNTIME_MOUNT} 挂出来，密钥仍然会随容器销毁"
    )


@pytest.mark.parametrize("name", ENTRYPOINTS)
def test_entrypoint_prepares_key_directory(name):
    text = (REPO_ROOT / name).read_text(encoding="utf-8")
    assert ENV_VAR in text, f"{name} 没有准备密钥目录"


def test_rotating_the_key_makes_stored_credentials_unreadable(monkeypatch):
    """说明后果：换了密钥，旧密文就再也解不开。"""
    from core.secret_box import SecretBox

    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
    sealed = SecretBox().encrypt_json({"session": "icloud-cookie"})

    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
    with pytest.raises(InvalidTag):
        SecretBox().decrypt_json(sealed)
