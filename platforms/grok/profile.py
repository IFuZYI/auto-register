"""注册资料生成（姓名 / 密码）。

出处：reference/grok/grokRegister-cpa/protocol_signup.py:200-201
"""
from __future__ import annotations

import random
import secrets

from .constants import FAMILY_NAMES, GIVEN_NAMES


def generate_password() -> str:
    """格式：N<8hex>!a7#<8urlsafe>，约 20 字符。

    与参考实现保持一致：大小写 + 数字 + 符号，满足 x.ai 密码强度要求。
    """
    return "N" + secrets.token_hex(4) + "!a7#" + secrets.token_urlsafe(6)


def random_name() -> tuple[str, str]:
    """返回 (given_name, family_name)。"""
    return random.choice(GIVEN_NAMES), random.choice(FAMILY_NAMES)
