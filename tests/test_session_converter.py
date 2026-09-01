import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sms_tool import cli
from sms_tool import session_converter as conv


def _jwt(payload):
    def part(value):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{part({'alg':'none'})}.{part(payload)}."


class SessionConverterTests(unittest.TestCase):
    def test_collect_nested_session_like_objects(self):
        access = _jwt({
            "exp": 1782973350,
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-1", "chatgpt_plan_type": "plus"},
            "https://api.openai.com/profile": {"email": "a@example.com", "user_id": "user-1"},
        })
        document = {"outer": {"items": [{"credentials": {"access_token": access, "chatgpt_account_id": "acct-1", "email": "a@example.com"}}]}}

        found = conv.collect_session_like_objects(document)

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["path"], "$.outer.items[0]")

    def test_convert_session_outputs_multiple_formats(self):
        access = _jwt({
            "exp": 1782973350,
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-1", "chatgpt_plan_type": "plus"},
            "https://api.openai.com/profile": {"email": "a@example.com", "user_id": "user-1"},
        })
        record = {
            "user": {"id": "user-1", "email": "a@example.com"},
            "account": {"id": "acct-1", "planType": "plus"},
            "accessToken": access,
            "sessionToken": "sess",
        }

        result = conv.convert_json_value({"sessions": [record]}, fmt="sub2api")

        self.assertEqual(len(result["converted"]), 1)
        self.assertEqual(result["output"]["accounts"][0]["credentials"]["chatgpt_account_id"], "acct-1")
        cpa = conv.build_output_document("cpa", result["converted"])
        self.assertEqual(cpa["account_id"], "acct-1")
        self.assertEqual(cpa["email"], "a@example.com")
        self.assertTrue(cpa["id_token_synthetic"])
        codex = conv.build_output_document("codex", result["converted"])
        self.assertEqual(codex["tokens"]["account_id"], "acct-1")
        axon = conv.build_output_document("axonhub", result["converted"])
        self.assertEqual(axon["tokens"]["refresh_token"], conv.AXONHUB_PLACEHOLDER_REFRESH_TOKEN)
        c2a = conv.build_output_document("chatgpt2api", result["converted"])
        self.assertIn("accounts", c2a)
        self.assertIsInstance(c2a["accounts"], list)
        self.assertEqual(len(c2a["accounts"]), 1)
        entry = c2a["accounts"][0]
        for field in ("access_token", "email", "user_id", "type", "source_type"):
            self.assertIn(field, entry)
        self.assertEqual(entry["type"], "plus")
        self.assertEqual(entry["source_type"], "web")
        self.assertEqual(entry["email"], "a@example.com")
        self.assertEqual(entry["user_id"], "user-1")

    def test_cli_convert_session_json_writes_output(self):
        access = _jwt({
            "exp": 1782973350,
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-2"},
            "https://api.openai.com/profile": {"email": "b@example.com", "user_id": "user-2"},
        })
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "input.json"
            out = Path(tmp) / "out.json"
            src.write_text(json.dumps({"nested": {"accessToken": access, "user": {"email": "b@example.com"}}}), encoding="utf-8")
            argv = [
                "chatgpt_phone_reg.py",
                "--convert-session-json",
                str(src),
                "--convert-format",
                "cpa",
                "--convert-output",
                str(out),
            ]
            with patch("sys.argv", argv):
                cli.main()

            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(data["account_id"], "acct-2")
            self.assertEqual(data["email"], "b@example.com")

    def test_cli_convert_chatgpt2api_writes_output(self):
        access = _jwt({
            "exp": 1782973350,
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-3", "chatgpt_plan_type": "unknown"},
            "https://api.openai.com/profile": {"email": "c@example.com", "user_id": "user-3"},
        })
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "input.json"
            out = Path(tmp) / "out.json"
            src.write_text(json.dumps({"nested": {"accessToken": access, "user": {"email": "c@example.com"}}}), encoding="utf-8")
            argv = [
                "chatgpt_phone_reg.py",
                "--convert-session-json",
                str(src),
                "--convert-format",
                "chatgpt2api",
                "--convert-output",
                str(out),
            ]
            with patch("sys.argv", argv):
                cli.main()

            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertIn("accounts", data)
            self.assertIsInstance(data["accounts"], list)
            self.assertEqual(len(data["accounts"]), 1)
            entry = data["accounts"][0]
            self.assertEqual(entry["type"], "free")
            self.assertEqual(entry["source_type"], "web")

    def test_chatgpt2api_excludes_missing_access_token(self):
        access = _jwt({
            "exp": 1782973350,
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-4"},
            "https://api.openai.com/profile": {"email": "d@example.com", "user_id": "user-4"},
        })
        good_record = {"accessToken": access, "email": "d@example.com"}
        bad_record = {"email": "no-token@example.com"}
        result = conv.convert_json_value({"sessions": [good_record, bad_record]}, fmt="chatgpt2api")
        output = result["output"]
        self.assertIsInstance(output, dict)
        self.assertEqual(len(output["accounts"]), 1)
        self.assertEqual(output["accounts"][0]["email"], "d@example.com")
        self.assertEqual(output["accounts"][0]["type"], "free")

    def test_chatgpt2api_build_output_filters_empty_token(self):
        with_token = {"chatgpt2api": {"access_token": "tok", "email": "a@b.c", "user_id": "u1", "type": "free", "source_type": "web"}}
        without_token = {"chatgpt2api": {"access_token": "", "email": "x@y.z", "user_id": "u2", "type": "free", "source_type": "web"}}
        result = conv.build_output_document("chatgpt2api", [with_token, without_token])
        self.assertEqual(len(result["accounts"]), 1)
        self.assertEqual(result["accounts"][0]["email"], "a@b.c")


if __name__ == "__main__":
    unittest.main()
