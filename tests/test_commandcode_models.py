import json
import os
import tempfile
import unittest
from unittest import mock

import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import update_commandcode_models as update_models  # noqa: E402


class UpdateModelsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = self.temp_dir.name
        self.paths = {
            "CONFIG_PATH": os.path.join(root, "opencode", "opencode.jsonc"),
            "AUTH_PATH": os.path.join(root, "opencode", "auth.json"),
            "ZCODE_PROVIDER_CONFIG_PATH": os.path.join(
                root, "zcode", "v2", "provider_config.json"
            ),
            "ZCODE_CLI_CONFIG_PATH": os.path.join(
                root, "zcode", "cli", "config.json"
            ),
        }
        self.path_patcher = mock.patch.multiple(update_models, **self.paths)
        self.path_patcher.start()
        self.addCleanup(self.path_patcher.stop)
        self.addCleanup(self.temp_dir.cleanup)

    def read_json(self, path):
        with open(path, "r", encoding="utf-8") as stream:
            return json.load(stream)

    def run_main(self, models, api_key="secret"):
        with mock.patch.object(update_models, "fetch_models", return_value=(models, {})), mock.patch(
            "builtins.input", return_value=api_key
        ):
            update_models.main()

    def test_updates_opencode_zcode_provider_and_cli_configs(self):
        models = {
            "alpha": {"name": "Alpha", "limit": {"context": 123, "output": 0}},
            "beta": {"name": "Beta"},
        }
        self.run_main(models)

        opencode = self.read_json(self.paths["CONFIG_PATH"])
        self.assertEqual(opencode["provider"]["commandcode"]["models"], models)

        provider_config = self.read_json(self.paths["ZCODE_PROVIDER_CONFIG_PATH"])
        self.assertEqual(provider_config["schemaVersion"], 1)
        rules = provider_config["config"]["providerConfigRules"]["providerRules"]
        self.assertEqual(len(rules), 1)
        rule = rules[0]
        self.assertEqual(rule["providerId"], "commandcode")
        self.assertEqual(rule["providerName"], "CommandCode")
        self.assertTrue(rule["enabled"])
        settings = rule["config"]
        self.assertEqual(settings["access"], {"type": "api-key", "apiKey": "secret"})
        self.assertEqual(
            settings["api"],
            {
                "type": "openai-chat-completions",
                "baseUrl": update_models.BASE_URL.rstrip("/"),
            },
        )
        self.assertEqual(settings["personalModelIds"], ["alpha", "beta"])
        self.assertEqual(settings["modelOrder"], ["alpha", "beta"])
        model_rules = provider_config["config"]["modelConfigRules"]["providerModelRules"]
        self.assertEqual(
            model_rules,
            [
                {
                    "modelId": "alpha",
                    "config": {"properties": {"contextWindow": 123}},
                    "providerId": "commandcode",
                }
            ],
        )
        self.assertEqual(
            provider_config["config"]["modelConfigRules"]["manualProviderModelRules"], []
        )

        cli = self.read_json(self.paths["ZCODE_CLI_CONFIG_PATH"])
        self.assertEqual(cli["model"]["main"], "commandcode/alpha")
        self.assertEqual(cli["provider"]["commandcode"]["options"]["apiKey"], "secret")

        auth = self.read_json(self.paths["AUTH_PATH"])
        self.assertEqual(auth["commandcode"]["key"], "secret")

    def test_writes_no_zcode_app_config_json(self):
        self.run_main({"alpha": {"name": "Alpha"}})

        directory = os.path.dirname(self.paths["ZCODE_PROVIDER_CONFIG_PATH"])
        self.assertEqual(sorted(os.listdir(directory)), ["provider_config.json"])

    def test_preserves_other_providers_and_existing_app_state(self):
        path = self.paths["ZCODE_PROVIDER_CONFIG_PATH"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(
                {
                    "schemaVersion": 1,
                    "config": {
                        "providerConfigRules": {
                            "providerRules": [
                                {
                                    "providerId": "other",
                                    "providerName": "Other",
                                    "enabled": True,
                                    "config": {"group": "standard-personal"},
                                },
                                {
                                    "providerId": "commandcode",
                                    "providerName": "CommandCode",
                                    "enabled": False,
                                    "config": {
                                        "group": "coding-plan",
                                        "access": {"type": "api-key", "apiKey": "old"},
                                        "api": {
                                            "type": "anthropic-messages",
                                            "baseUrl": "https://old.example/v1",
                                        },
                                        "personalModelIds": ["beta", "gone"],
                                        "modelOrder": ["beta", "gone"],
                                    },
                                },
                            ]
                        },
                        "modelConfigRules": {
                            "providerModelRules": [
                                {
                                    "modelId": "beta",
                                    "config": {
                                        "properties": {"contextWindow": 1, "maxOutputTokens": 9}
                                    },
                                    "providerId": "commandcode",
                                },
                                {
                                    "modelId": "other-model",
                                    "config": {"properties": {}},
                                    "providerId": "other",
                                },
                            ],
                            "manualProviderModelRules": [
                                {"modelId": "manual", "config": {}, "providerId": "commandcode"}
                            ],
                        },
                    },
                },
                stream,
            )

        models = {
            "alpha": {"name": "Alpha", "limit": {"context": 123}},
            "beta": {"name": "Beta", "limit": {"context": 456}},
        }
        self.run_main(models)

        provider_config = self.read_json(path)
        rules = provider_config["config"]["providerConfigRules"]["providerRules"]
        self.assertEqual([rule["providerId"] for rule in rules], ["other", "commandcode"])
        settings = rules[1]["config"]
        self.assertFalse(rules[1]["enabled"])
        self.assertEqual(settings["group"], "coding-plan")
        self.assertEqual(
            settings["api"],
            {"type": "openai-chat-completions", "baseUrl": update_models.ZCODE_BASE_URL},
        )
        self.assertEqual(settings["access"]["apiKey"], "secret")
        self.assertEqual(settings["personalModelIds"], ["beta", "alpha"])
        self.assertEqual(settings["modelOrder"], ["beta", "alpha"])

        model_rules = provider_config["config"]["modelConfigRules"]["providerModelRules"]
        self.assertEqual(
            model_rules,
            [
                {
                    "modelId": "alpha",
                    "config": {"properties": {"contextWindow": 123}},
                    "providerId": "commandcode",
                },
                {
                    "modelId": "beta",
                    "config": {
                        "properties": {"contextWindow": 456, "maxOutputTokens": 9}
                    },
                    "providerId": "commandcode",
                },
                {
                    "modelId": "other-model",
                    "config": {"properties": {}},
                    "providerId": "other",
                },
            ],
        )
        self.assertEqual(
            provider_config["config"]["modelConfigRules"]["manualProviderModelRules"],
            [{"modelId": "manual", "config": {}, "providerId": "commandcode"}],
        )

    def test_preserves_existing_cli_default_and_unrelated_provider(self):
        os.makedirs(os.path.dirname(self.paths["ZCODE_CLI_CONFIG_PATH"]), exist_ok=True)
        with open(self.paths["ZCODE_CLI_CONFIG_PATH"], "w", encoding="utf-8") as stream:
            json.dump(
                {
                    "provider": {"other": {"models": {"keep": {}}}},
                    "model": {"main": "other/keep"},
                },
                stream,
            )

        self.run_main({"alpha": {"name": "Alpha"}})

        cli = self.read_json(self.paths["ZCODE_CLI_CONFIG_PATH"])
        self.assertEqual(cli["model"]["main"], "other/keep")
        self.assertIn("other", cli["provider"])

    def test_replaces_invalid_cli_default(self):
        os.makedirs(os.path.dirname(self.paths["ZCODE_CLI_CONFIG_PATH"]), exist_ok=True)
        with open(self.paths["ZCODE_CLI_CONFIG_PATH"], "w", encoding="utf-8") as stream:
            json.dump(
                {
                    "provider": {"other": {"models": {"keep": {}}}},
                    "model": {"main": "other/missing"},
                },
                stream,
            )

        self.run_main({"alpha": {"name": "Alpha"}})

        cli = self.read_json(self.paths["ZCODE_CLI_CONFIG_PATH"])
        self.assertEqual(cli["model"]["main"], "commandcode/alpha")

    def test_rejects_empty_catalog_without_writing(self):
        with mock.patch.object(update_models, "fetch_models", return_value=({}, {})), mock.patch(
            "builtins.input"
        ) as input_mock:
            with self.assertRaises(RuntimeError):
                update_models.main()

        input_mock.assert_not_called()
        self.assertFalse(os.path.exists(self.paths["CONFIG_PATH"]))
        self.assertFalse(os.path.exists(self.paths["ZCODE_PROVIDER_CONFIG_PATH"]))

    def test_rejects_malformed_zcode_provider_config_before_writing_other_files(self):
        path = self.paths["ZCODE_PROVIDER_CONFIG_PATH"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("not json")

        with mock.patch.object(update_models, "fetch_models", return_value=({"alpha": {"name": "Alpha"}}, {})), mock.patch(
            "builtins.input", return_value="secret"
        ):
            with self.assertRaises(json.JSONDecodeError):
                update_models.main()

        self.assertFalse(os.path.exists(self.paths["CONFIG_PATH"]))
        self.assertFalse(os.path.exists(self.paths["AUTH_PATH"]))


if __name__ == "__main__":
    unittest.main()
