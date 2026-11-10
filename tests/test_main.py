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
        sender_name=None, sender_email=None, sender_smtp=None, body_text=None,
        client_submit_time=None, delivery_time=None,
        get=lambda key: 65001, store=SimpleNamespace(codepage="cp1252"),
        body_html=None, body_rtf_decompressed=lambda: None,
        recipients=lambda: [], attachments=lambda: [], node="1",
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


class SearchTests(unittest.TestCase):
    def test_searches_only_visible_fields_and_counts_message_once(self):
        messages = [
            fake_message(body_text="also 1231231231", node="1"),
            fake_message(subject="S1231231231", node="2"),
            fake_message({0x7777: "hidden 1231231231 metadata"}, node="3"),
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
        self.assertEqual(matches[0].matched_fields, ["Text"])

    def test_uses_one_readable_body_without_html_source_or_duplicate_rtf(self):
        message = fake_message(
            body_html="<style>hidden</style><p>S123<b>123</b>123&#49;</p><p>Привіт</p>".encode(),
            body_rtf_decompressed=lambda: br"{\rtf1\ansi S123{\b 123}1231}",
        )
        fields = dict(main.message_fields(message))
        self.assertIn("S1231231231", fields["Text"])
        self.assertIn("Привіт", fields["Text"])
        self.assertNotIn("hidden", fields["Text"])
        self.assertEqual(set(fields), {"Subject", "Text"})

    def test_undefined_cp1252_byte_does_not_abort_html_scan(self):
        message = fake_message(body_html=b"before \x90 S1231231231 after", get=lambda key: 1252)
        fields = dict(main.message_fields(message))
        self.assertEqual(fields["Text"], "before \ufffd S1231231231 after")

    def test_keeps_sender_recipients_and_dates(self):
        attachment = SimpleNamespace(
            properties=Properties({0x3707: "hidden 1231231231.txt"}), embedded_message=lambda: None,
        )
        recipient = SimpleNamespace(
            type=1, name="Recipient", email="legacy@example.com",
            smtp="recipient@example.com", properties={},
        )
        fields = dict(main.message_fields(fake_message(
            sender_name="Sender", sender_email="legacy-sender@example.com",
            sender_smtp="sender@example.com", recipients=lambda: [recipient],
            client_submit_time=datetime(2026, 1, 2, 3, 4, 5),
            delivery_time=datetime(2026, 1, 2, 3, 5, 6), attachments=lambda: [attachment],
        )))
        self.assertEqual(fields["From"], "Sender <sender@example.com>")
        self.assertEqual(fields["To"], "Recipient <recipient@example.com>")
        self.assertEqual(fields["Sent"], "2026-01-02 03:04:05")
        self.assertEqual(fields["Received"], "2026-01-02 03:05:06")
        self.assertFalse(any("Attachment" in label for label in fields))

    def test_no_matching_across_unrelated_fields_and_case_insensitivity(self):
        msg = fake_message(subject="ABC", body_text="def")
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
        self.match = main.Match("Inbox", "1", [("Text", "Привіт S1231231231")], ["Text"])

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
            text = first.read_text(encoding="utf-8")
            self.assertIn("Привіт S1231231231", text)
            self.assertNotIn("Folder:", text)
            self.assertNotIn("Message ID:", text)
            self.assertNotIn("Matched fields:", text)


FIXTURES = main.PROJECT_DIR / ".test-data"


class RealPstTests(unittest.TestCase):
    @unittest.skipUnless((FIXTURES / "tika-variousBodyTypes.pst").exists(), "Optional public PST fixture")
    def test_real_pst_all_folders_body_formats_and_no_matches(self):
        path = FIXTURES / "tika-variousBodyTypes.pst"
        matches = main.find_matches(main.Settings(path, "original email"))
        self.assertEqual(len(matches), 4)
        self.assertTrue(all("Text" in m.matched_fields for m in matches))
        self.assertTrue(all(set(label for label, _ in m.fields) <= {
            "Subject", "From", "To", "Cc", "Bcc", "Sent", "Received", "Text"
        } for m in matches))
        self.assertEqual(main.find_matches(main.Settings(path, "nonexistent-code-1231231231")), [])

    @unittest.skipUnless((FIXTURES / "pstsdk-submessage.pst").exists(), "Optional public PST fixture")
    def test_real_pst_embedded_email(self):
        path = FIXTURES / "pstsdk-submessage.pst"
        matches = main.find_matches(main.Settings(path, "message"))
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].matched_fields, ["Subject", "Text"])


if __name__ == "__main__":
    unittest.main()
