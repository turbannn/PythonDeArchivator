from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import main


class Properties:
    def __init__(self, values):
        self.values = values
        self.records = {
            key: SimpleNamespace(is_null=False, value_type=0x001F, key=key)
            for key in values
        }

    def read(self, record):
        return self.values[record.key]


def fake_message(values=None, **overrides):
    fields = dict(
        subject="Example", properties=Properties(values or {}),
        client_submit_time=None, delivery_time=None,
        get=lambda key: 65001, store=SimpleNamespace(codepage="cp1252"),
        body_html=None, body_rtf_decompressed=lambda: None,
        recipients=lambda: [], attachments=lambda: [], node="1",
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


class SearchTests(unittest.TestCase):
    def test_substring_in_all_text_fields_counts_message_once(self):
        messages = [
            fake_message({0x0037: "S1231231231", 0x1000: "also 1231231231"}, node="1"),
            fake_message({0x7777: "Custom 1231231231 metadata"}, node="2"),
            fake_message({0x1000: "not a match"}, node="3"),
        ]
        root = SimpleNamespace(messages=lambda: iter(messages[:1]))
        nested = SimpleNamespace(messages=lambda: iter(messages[1:]))
        with patch.object(main.pypstreader, "open") as opened, patch.object(
            main, "folder_paths", return_value=[(root, "Root"), (nested, "Root/Inbox/Nested")]
        ):
            opened.return_value.__enter__.return_value.root_folder = root
            matches = main.find_matches(main.Settings(Path("unused.pst"), "1231231231"))
        self.assertEqual(len(matches), 2)
        self.assertEqual(matches[1].folder, "Root/Inbox/Nested")
        self.assertEqual(matches[0].matched_fields, ["Subject", "Body"])

    def test_formatted_html_unicode_and_rtf(self):
        message = fake_message(
            body_html="<style>hidden</style><p>S123<b>123</b>123&#49;</p><p>Привіт</p>".encode(),
            body_rtf_decompressed=lambda: br"{\rtf1\ansi S123{\b 123}1231}",
        )
        fields = dict(main.message_fields(message))
        self.assertIn("S1231231231", fields["HTML text"])
        self.assertIn("Привіт", fields["HTML text"])
        self.assertNotIn("hidden", fields["HTML text"])
        self.assertIn("S1231231231", fields["Rich text"])

    def test_encoded_header(self):
        fields = dict(main.message_fields(fake_message({0x0037: "=?utf-8?b?UzEyMzEyMzEyMzE=?="})))
        self.assertEqual(fields["Subject (decoded)"], "S1231231231")

    def test_undefined_cp1252_byte_does_not_abort_html_scan(self):
        message = fake_message(body_html=b"before \x90 S1231231231 after", get=lambda key: 1252)
        fields = dict(main.message_fields(message))
        self.assertEqual(fields["HTML text"], "before \ufffd S1231231231 after")

    def test_text_attachment_uses_store_codepage_and_replaces_bad_byte(self):
        attachment = SimpleNamespace(
            properties=Properties({}), embedded_message=lambda: None,
            long_filename="example.txt", filename=None, mime_tag="text/plain",
            data=lambda: b"caf\xe9 \x90 1231231231",
        )
        fields = dict(main.message_fields(fake_message(attachments=lambda: [attachment])))
        self.assertEqual(fields["Attachment 1 / Text content"], "caf\u00e9 \ufffd 1231231231")

    def test_recipient_text_attachment_and_embedded_email(self):
        embedded = fake_message({0x1000: "embedded 1231231231"})
        text = SimpleNamespace(
            properties=Properties({0x3707: "S1231231231.txt"}),
            embedded_message=lambda: None, long_filename="S1231231231.txt",
            filename=None, mime_tag="text/plain", data=lambda: b"attached 1231231231",
        )
        attached_email = SimpleNamespace(properties=Properties({}), embedded_message=lambda: embedded)
        fields = dict(main.message_fields(fake_message(
            recipients=lambda: [SimpleNamespace(properties={0x3003: "1231231231@example.com"})],
            attachments=lambda: [text, attached_email],
        )))
        self.assertIn("1231231231", fields["Recipient 1 / 0x3003"])
        self.assertIn("1231231231", fields["Attachment 1 / Text content"])
        self.assertIn("1231231231", fields["Attachment 2 / Embedded email / Body"])

    def test_no_matching_across_unrelated_fields_and_case_insensitivity(self):
        msg = fake_message({0x0037: "ABC", 0x1000: "def"})
        folder = SimpleNamespace(messages=lambda: iter([msg]))
        with patch.object(main.pypstreader, "open"), patch.object(
            main, "folder_paths", side_effect=lambda *args, **kwargs: [(folder, "Inbox")]
        ):
            self.assertEqual(main.find_matches(main.Settings(Path("unused.pst"), "abcdef")), [])
            self.assertEqual(len(main.find_matches(main.Settings(Path("unused.pst"), "abc"))), 1)


class ConfigTests(unittest.TestCase):
    def test_absolute_path_quotes_bom_and_literal_characters(self):
        with TemporaryDirectory(dir=main.PROJECT_DIR) as tmp:
            pst = Path(tmp) / "my archive.pst"
            pst.touch()
            config = Path(tmp) / "env.txt"
            config.write_text(f'\ufeff# Settings\nPST_PATH="{pst}"\nSEARCH_TEXT=" A#B=C "\n', encoding="utf-8")
            settings = main.read_settings(config)
            self.assertEqual(settings.pst_path, pst)
            self.assertEqual(settings.search_text, " A#B=C ")

    def test_invalid_configs(self):
        cases = [
            "PST_PATH=x.pst\nSEARCH_TEXT=", "PST_PATH=x.pst\nSEARCH_TEXT=abc",
            "PST_PATH=x.pst\nSEARCH_TEXT=a\nSEARCH_TEXT=b", "incorrect line",
            'PST_PATH="unterminated\nSEARCH_TEXT=abc', "SEARCH_TEXT=abc",
        ]
        with TemporaryDirectory(dir=main.PROJECT_DIR) as tmp:
            config = Path(tmp) / "env.txt"
            for value in cases:
                with self.subTest(value=value):
                    config.write_text(value, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        main.read_settings(config)


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.settings = main.Settings(Path("archive.pst"), "1231231231")
        self.match = main.Match("Inbox", "1", [("Body", "Привіт S1231231231")], ["Body"])

    def run_console(self, matches, answer="", error=None):
        out, err = StringIO(), StringIO()
        with patch.object(main, "read_settings", return_value=self.settings), patch.object(
            main, "find_matches", return_value=matches, side_effect=error
        ), patch("builtins.input", return_value=answer) as prompt, patch.object(
            main, "write_results", return_value=Path("results.txt")
        ) as save, redirect_stdout(out), redirect_stderr(err):
            result = main.main()
        return result, out.getvalue(), err.getvalue(), prompt, save

    def test_zero_matches_never_prompts(self):
        code, out, _, prompt, save = self.run_console([])
        self.assertEqual(code, 0)
        self.assertEqual(out, "No emails found containing this text.\n")
        prompt.assert_not_called()
        save.assert_not_called()

    def test_default_and_no_decline_export(self):
        for answer in ("", "N", "n", "anything"):
            with self.subTest(answer=answer):
                _, _, _, prompt, save = self.run_console([self.match], answer)
                prompt.assert_called_once_with(
                    "found 1 emails containing this text do you want to write results to file? [ y | N ] "
                )
                save.assert_not_called()

    def test_yes_exports(self):
        for answer in ("y", "Y", " y "):
            _, _, _, _, save = self.run_console([self.match], answer)
            save.assert_called_once()

    def test_corrupt_pst_does_not_report_no_matches_or_prompt(self):
        code, out, err, prompt, save = self.run_console([], error=main.pypstreader.PstFormatError("bad PST"))
        self.assertEqual(code, 1)
        self.assertNotIn("No emails found", out)
        self.assertIn("did not complete", err)
        prompt.assert_not_called()
        save.assert_not_called()

    def test_permission_error_without_filename_reports_pst_and_lock_guidance(self):
        code, out, err, prompt, save = self.run_console([], error=PermissionError(13, "Permission denied"))
        self.assertEqual(code, 1)
        self.assertIn("reading the PST: archive.pst", err)
        self.assertIn("Close Outlook completely", err)
        self.assertIn("not a PST password error", err)
        self.assertNotIn("No emails found", out)
        prompt.assert_not_called()
        save.assert_not_called()

    def test_export_permission_error_does_not_blame_pst(self):
        err = StringIO()
        with patch.object(main, "read_settings", return_value=self.settings), patch.object(
            main, "find_matches", return_value=[self.match]
        ), patch("builtins.input", return_value="y"), patch.object(
            main, "write_results", side_effect=PermissionError(13, "Permission denied")
        ), redirect_stderr(err):
            self.assertEqual(main.main(), 1)
        self.assertIn("writing results", err.getvalue())
        self.assertIn("Check write permission", err.getvalue())
        self.assertNotIn("Close Outlook", err.getvalue())

    def test_export_unicode_and_collision(self):
        with TemporaryDirectory(dir=main.PROJECT_DIR) as tmp, patch.object(main, "datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 22, 14, 30, 5)
            first = main.write_results([self.match], self.settings, Path(tmp))
            original = first.read_bytes()
            second = main.write_results([self.match], self.settings, Path(tmp))
            self.assertEqual(first.name, "14-30-05_2026-10-22.txt")
            self.assertEqual(second.name, "14-30-05_2026-10-22_1.txt")
            self.assertEqual(first.read_bytes(), original)
            self.assertIn("Привіт S1231231231", first.read_text(encoding="utf-8"))


FIXTURES = main.PROJECT_DIR / ".test-data"


class RealPstTests(unittest.TestCase):
    @unittest.skipUnless((FIXTURES / "tika-variousBodyTypes.pst").exists(), "Optional public PST fixture")
    def test_real_pst_all_folders_body_formats_and_no_matches(self):
        path = FIXTURES / "tika-variousBodyTypes.pst"
        matches = main.find_matches(main.Settings(path, "original email"))
        self.assertEqual(len(matches), 4)
        self.assertTrue(any("Rich text" in m.matched_fields for m in matches))
        self.assertTrue(any("HTML text" in m.matched_fields for m in matches))
        self.assertEqual(main.find_matches(main.Settings(path, "nonexistent-code-1231231231")), [])

    @unittest.skipUnless((FIXTURES / "pstsdk-submessage.pst").exists(), "Optional public PST fixture")
    def test_real_pst_embedded_email(self):
        path = FIXTURES / "pstsdk-submessage.pst"
        matches = main.find_matches(main.Settings(path, "message"))
        self.assertEqual(len(matches), 1)
        self.assertTrue(any("Embedded email" in field for field in matches[0].matched_fields))


if __name__ == "__main__":
    unittest.main()
