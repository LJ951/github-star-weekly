import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from src.emailer import DatabaseSendRecorder, _build_resend_headers, send_weekly_email


class Recorder:
    def __init__(self, already_sent=False):
        self.already_sent = already_sent
        self.records = []

    def email_already_sent(self, week_start):
        return self.already_sent

    def record_email_sent(self, **kwargs):
        self.records.append(kwargs)


class EmailerTests(unittest.TestCase):
    def test_skips_when_week_already_sent_and_not_forced(self):
        recorder = Recorder(already_sent=True)

        result = send_weekly_email(
            html="<p>report</p>",
            subject="Weekly",
            sender="report@example.com",
            recipients="reader@example.com",
            api_key="secret",
            week_start="2026-05-11",
            week_end="2026-05-17",
            recorder=recorder,
        )

        self.assertFalse(result.sent)
        self.assertTrue(result.skipped)
        self.assertEqual(recorder.records, [])

    @mock.patch("src.emailer._send_with_resend_http", side_effect=RuntimeError("resend down"))
    def test_send_failure_does_not_record_email(self, _send):
        recorder = Recorder()

        with self.assertRaises(RuntimeError):
            send_weekly_email(
                html="<p>report</p>",
                subject="Weekly",
                sender="report@example.com",
                recipients=["reader@example.com"],
                api_key="secret",
                week_start="2026-05-11",
                week_end="2026-05-17",
                recorder=recorder,
            )

        self.assertEqual(recorder.records, [])

    @mock.patch("src.emailer._send_with_resend_http", return_value="email_123")
    def test_success_records_after_resend_accepts_request(self, _send):
        recorder = Recorder()

        result = send_weekly_email(
            html="<p>report</p>",
            subject="Weekly",
            sender="report@example.com",
            recipients="reader@example.com, second@example.com",
            api_key="secret",
            week_start="2026-05-11",
            week_end="2026-05-17",
            recorder=recorder,
        )

        self.assertTrue(result.sent)
        self.assertEqual(result.message_id, "email_123")
        self.assertEqual(len(recorder.records), 1)
        self.assertEqual(recorder.records[0]["recipient"], "reader@example.com,second@example.com")

    def test_missing_secret_validation_names_settings_without_leaking_values(self):
        with self.assertRaisesRegex(ValueError, "RESEND_API_KEY"):
            send_weekly_email(
                html="<p>report</p>",
                subject="Weekly",
                sender="report@example.com",
                recipients="reader@example.com",
                api_key="",
            )

    @mock.patch("src.emailer._send_with_resend_http", return_value="email_456")
    def test_database_recorder_integrates_with_db_module(self, _send):
        with TemporaryDirectory() as temp_dir:
            recorder = DatabaseSendRecorder(Path(temp_dir) / "rankings.sqlite")

            first = send_weekly_email(
                html="<p>report</p>",
                subject="Weekly",
                sender="report@example.com",
                recipients="reader@example.com",
                api_key="secret",
                week_start="2026-05-11",
                week_end="2026-05-17",
                recorder=recorder,
            )
            second = send_weekly_email(
                html="<p>report</p>",
                subject="Weekly",
                sender="report@example.com",
                recipients="reader@example.com",
                api_key="secret",
                week_start="2026-05-11",
                week_end="2026-05-17",
                recorder=recorder,
            )

        self.assertTrue(first.sent)
        self.assertTrue(second.skipped)

    def test_build_resend_headers_includes_bounded_idempotency_key(self):
        headers = _build_resend_headers("secret", "x" * 300)

        self.assertEqual(headers["Authorization"], "Bearer secret")
        self.assertEqual(len(headers["Idempotency-Key"]), 256)


if __name__ == "__main__":
    unittest.main()
