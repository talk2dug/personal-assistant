"""Unit tests for the junk/spam heuristic classifier. Pure functions, no I/O,
no mocking needed.
"""
from assistant.core import junk_filter


def test_obvious_spam_is_flagged():
    headers = {"from": "deals@offers.xyz", "subject": "WINNER!!! Click here now!!!"}
    assert junk_filter.is_junk(headers) is True


def test_ordinary_email_is_not_flagged():
    headers = {"from": "Mom <mom@family.com>", "subject": "Dinner Sunday?"}
    assert junk_filter.is_junk(headers) is False


def test_score_counts_independent_signals():
    headers = {"from": "deals@offers.xyz", "subject": "WINNER!!! ACT NOW CLICK HERE!!!"}
    assert junk_filter.score(headers) >= 2


def test_shouting_subject_alone_is_a_signal():
    headers = {"from": "coworker@work.com", "subject": "PLEASE REVIEW THIS DOCUMENT TODAY"}
    assert junk_filter.score(headers) >= 1


def test_repeated_re_prefix_is_a_signal():
    headers = {"from": "someone@example.com", "subject": "Re: Re: Re: your invoice"}
    assert junk_filter.score(headers) >= 1


def test_higher_threshold_requires_more_signals():
    headers = {"from": "someone@example.com", "subject": "Re: Re: Re: your invoice"}
    assert junk_filter.is_junk(headers, threshold=1) is True
    assert junk_filter.is_junk(headers, threshold=2) is False


def test_missing_fields_do_not_crash():
    assert junk_filter.is_junk({}) is False


def test_suspicious_tld_alone_is_a_signal():
    headers = {"from": "noreply@updates.ru", "subject": "Your recent activity"}
    assert junk_filter.score(headers) >= 1
