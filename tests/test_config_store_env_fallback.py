import tempfile
import unittest
from pathlib import Path

from core.config_store import (
    _canonical_config_key,
    _get_env_fallback_value,
    _load_env_file,
    _merge_env_fallback,
    _normalize_config_value,
)


class ConfigStoreEnvFallbackTests(unittest.TestCase):
    def test_normalize_config_value_strips_matching_quotes(self):
        self.assertEqual(_normalize_config_value('"quoted"'), "quoted")
        self.assertEqual(_normalize_config_value("'quoted'"), "quoted")
        self.assertEqual(_normalize_config_value("plain"), "plain")

    def test_load_env_file_supports_export_and_quotes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "# comment",
                        "export SMS_API_KEY='sms-key-demo'",
                        'cfworker_custom_auth="secret-pass"',
                    ]
                ),
                encoding="utf-8",
            )

            values = _load_env_file(env_path)

        self.assertEqual(values["SMS_API_KEY"], "sms-key-demo")
        self.assertEqual(values["cfworker_custom_auth"], "secret-pass")

    def test_get_env_fallback_value_matches_uppercase_env_names(self):
        env_values = {
            "SMS_API_KEY": "sms-key-demo",
            "CFWORKER_CUSTOM_AUTH": "secret-pass",
        }

        self.assertEqual(
            _get_env_fallback_value("sms_api_key", env_values=env_values),
            "sms-key-demo",
        )
        self.assertEqual(
            _get_env_fallback_value("cfworker_custom_auth", env_values=env_values),
            "secret-pass",
        )

    def test_merge_env_fallback_uses_canonical_key_without_overriding_db(self):
        merged = _merge_env_fallback(
            {
                "sms_api_key": "",
                "cfworker_custom_auth": "db-value",
            },
            env_values={
                "SMS_API_KEY": "sms-key-demo",
                "CFWORKER_CUSTOM_AUTH": "env-value",
            },
        )

        self.assertEqual(_canonical_config_key("SMS_API_KEY"), "sms_api_key")
        self.assertEqual(merged["sms_api_key"], "sms-key-demo")
        self.assertEqual(merged["cfworker_custom_auth"], "db-value")


if __name__ == "__main__":
    unittest.main()
