"""AuthFlow 的方法分组（Mixin）。

`AuthFlow` 自身 76 个方法 + PhoneRegisterMixin 24 个，同名冲突为 0，因此可以安全
按职责切成 Mixin。MRO 顺序即依赖顺序：`PhoneRegisterMixin` 放最前（保持现状行为），
其余按「后定义的可覆盖前面的」排。

拆分的硬约束（见 tests/test_auth_flow_module_patching.py）：
* 每个搬走的模块**必须各自 import `time` 与 `create_http_session`**，否则
  `mock.patch.object(auth_flow_module.time, "sleep")` /
  `mock.patch.object(auth_flow_module, "create_http_session")` 打桩会失效。
* `AuthFlow.__new__(AuthFlow)` 的测试只装部分属性，搬动时不能引入新的 `self._foo` 假设。
"""
from platforms.chatgpt.protocol.mixins.add_phone import AddPhoneMixin  # noqa: F401
from platforms.chatgpt.protocol.mixins.codex import CodexMixin  # noqa: F401
from platforms.chatgpt.protocol.mixins.redirect import RedirectMixin  # noqa: F401
from platforms.chatgpt.protocol.mixins.session import SessionMixin  # noqa: F401
from platforms.chatgpt.protocol.mixins.signup import SignupMixin  # noqa: F401
from platforms.chatgpt.protocol.mixins.trace import TraceMixin  # noqa: F401

__all__ = ["AddPhoneMixin", "CodexMixin", "RedirectMixin", "SessionMixin", "SignupMixin", "TraceMixin"]
