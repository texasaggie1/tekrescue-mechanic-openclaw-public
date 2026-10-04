"""Tests for the notifier: Telegram plain text and chunking."""

from __future__ import annotations

import unittest
from unittest import mock

from mechanic import notifier


def _config() -> mock.Mock:
    cfg = mock.Mock()
    cfg.notifier.kind = "telegram"
    cfg.notifier.telegram_bot_token = "123:abc"
    cfg.notifier.telegram_chat_id = "42"
    return cfg


class TelegramTests(unittest.TestCase):
    def test_report_goes_out_as_plain_text_without_parse_mode(self) -> None:
        # Backticks, underscores and asterisks: the Markdown parser used to
        # reject this with HTTP 400 "can't parse entities".
        body = "Update: skipped\nSet HERMES_GATEWAY_AUTOHEAL=true or run `mechanic status` *now*"
        with mock.patch("mechanic.notifier.requests.post") as post:
            post.return_value = mock.Mock(status_code=200, text="ok")
            result = notifier.send(_config(), "tekRESCUE Mechanic: success", body)
        self.assertTrue(result.delivered)
        self.assertEqual(post.call_count, 1)
        payload = post.call_args.kwargs["json"]
        self.assertNotIn("parse_mode", payload)
        self.assertEqual(payload["chat_id"], "42")
        self.assertEqual(payload["text"], "tekRESCUE Mechanic: success\n\n" + body)
        self.assertIn("bot123:abc/sendMessage", post.call_args.args[0])

    def test_long_report_is_split_under_the_telegram_limit(self) -> None:
        body = "\n".join(f"line {i:04d} " + "x" * 60 for i in range(120))
        with mock.patch("mechanic.notifier.requests.post") as post:
            post.return_value = mock.Mock(status_code=200, text="ok")
            result = notifier.send(_config(), "subject", body)
        self.assertTrue(result.delivered)
        self.assertGreater(post.call_count, 1)
        texts = [c.kwargs["json"]["text"] for c in post.call_args_list]
        for index, text in enumerate(texts):
            self.assertLessEqual(len(text), 4096)
            self.assertTrue(text.startswith(f"({index + 1}/{len(texts)}) "))
        rejoined = "".join(t.split(") ", 1)[1] for t in texts)
        self.assertEqual(rejoined, "subject\n\n" + body)

    def test_http_error_is_reported_not_raised(self) -> None:
        with mock.patch("mechanic.notifier.requests.post") as post:
            post.return_value = mock.Mock(status_code=400, text='{"ok":false}')
            result = notifier.send(_config(), "subject", "body")
        self.assertFalse(result.delivered)
        self.assertIn("HTTP 400", result.error)


class SplitTests(unittest.TestCase):
    def test_splits_on_line_boundaries(self) -> None:
        text = "aaaa\nbbbb\ncccc\n"
        chunks = notifier.split_message(text, 10)
        self.assertEqual(chunks, ["aaaa\nbbbb\n", "cccc\n"])
        self.assertEqual("".join(chunks), text)

    def test_single_overlong_line_is_cut(self) -> None:
        chunks = notifier.split_message("x" * 25, 10)
        self.assertEqual(chunks, ["x" * 10, "x" * 10, "x" * 5])

    def test_short_text_is_one_chunk(self) -> None:
        self.assertEqual(notifier.split_message("hi", 10), ["hi"])


if __name__ == "__main__":
    unittest.main()
