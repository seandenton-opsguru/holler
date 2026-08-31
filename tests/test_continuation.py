import holler


def test_window_constant():
    assert holler.CONTINUATION_SEC == 15.0


def test_within_window_gets_space():
    assert holler.needs_continuation_space("How are you?", now=100.0, last_ts=95.0)
    assert holler.needs_continuation_space("How are you?", now=100.0, last_ts=85.0)  # exactly 15 s


def test_after_window_no_space():
    assert not holler.needs_continuation_space("How are you?", now=100.0, last_ts=84.0)


def test_first_paste_no_space():
    assert not holler.needs_continuation_space("How are you?", now=100.0, last_ts=None)


def test_punctuation_leading_no_space():
    assert not holler.needs_continuation_space(". Anyway", now=100.0, last_ts=95.0)
    assert not holler.needs_continuation_space("— right", now=100.0, last_ts=95.0)


def test_digit_leading_gets_space():
    assert holler.needs_continuation_space("42 is the answer", now=100.0, last_ts=95.0)


def test_empty_transcript_no_space():
    assert not holler.needs_continuation_space("", now=100.0, last_ts=95.0)
