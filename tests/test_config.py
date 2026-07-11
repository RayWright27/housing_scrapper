"""Config parsing tests — pure, no I/O beyond monkeypatched env vars."""

from __future__ import annotations

import config


def test_chat_ids_parse_single_and_multiple(monkeypatch) -> None:
    # A plain single id keeps working (backward compatible).
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111111111")
    assert config._get_list("TELEGRAM_CHAT_ID") == ("111111111",)

    # Comma / semicolon / whitespace all separate, with stray spaces trimmed.
    monkeypatch.setenv("TELEGRAM_CHAT_ID", " 111 , 222 ; 333 444 ")
    assert config._get_list("TELEGRAM_CHAT_ID") == ("111", "222", "333", "444")


def test_chat_ids_empty_or_unset_is_empty_tuple(monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert config._get_list("TELEGRAM_CHAT_ID") == ()
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "   ")
    assert config._get_list("TELEGRAM_CHAT_ID") == ()
