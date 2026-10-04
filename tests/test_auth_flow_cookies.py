import unittest
from types import SimpleNamespace

from platforms.chatgpt.protocol.auth_flow import AuthFlow


class AuthFlowCookieTests(unittest.TestCase):
    def test_cookie_lookup_prefers_chatgpt_domain_when_name_is_duplicated(self):
        flow = AuthFlow.__new__(AuthFlow)
        flow.session = SimpleNamespace(
            cookies=[
                SimpleNamespace(name="oai-did", value="openai-value", domain=".openai.com"),
                SimpleNamespace(name="oai-did", value="chatgpt-value", domain=".chatgpt.com"),
            ]
        )

        self.assertEqual(flow._get_cookie_value_by_name("oai-did"), "chatgpt-value")


if __name__ == "__main__":
    unittest.main()
