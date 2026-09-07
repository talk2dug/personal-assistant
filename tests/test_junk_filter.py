"""Pure unit tests for the junk/spam heuristic scorer -- no IMAP, no network."""
from assistant.core import junk_filter


def test_ordinary_email_scores_low_and_is_not_junk():
    sender = "Jamie <jamie@example.com>"
    subject = "Your order shipped"
    body = "Hi, just letting you know your order shipped today!"

    verdict = junk_filter.score_message(sender, subject, body)

    assert verdict["score"] < junk_filter.DEFAULT_THRESHOLD
    assert not junk_filter.is_junk(sender, subject, body)


def test_obvious_phishing_scores_high_and_is_junk():
    sender = '"PayPal Security" <alerts@totally-not-paypal.tk>'
    subject = "URGENT: Verify your account NOW!!!"
    body = "Click here immediately to verify your account or it will be suspended."

    verdict = junk_filter.score_message(sender, subject, body)

    assert verdict["score"] >= junk_filter.DEFAULT_THRESHOLD
    assert junk_filter.is_junk(sender, subject, body)
    assert any("verify your account" in r for r in verdict["reasons"])
    assert any("suspended your account" in r for r in verdict["reasons"])
    assert any("all caps" in r for r in verdict["reasons"])
    assert any("paypal" in r and "totally-not-paypal.tk" in r for r in verdict["reasons"])


def test_prize_scam_scores_high():
    verdict = junk_filter.score_message(
        "noreply@random-promo.example", "You've won a prize!!",
        "Congratulations you have won a gift card, claim your prize now, this is not a scam.",
    )
    assert verdict["score"] >= junk_filter.DEFAULT_THRESHOLD


def test_brand_impersonation_flagged_even_without_spam_keywords():
    verdict = junk_filter.score_message(
        '"Apple Support" <support@totally-different-domain.ru>',
        "About your recent purchase", "See attached invoice for your records.",
    )
    assert any("claims 'apple'" in r for r in verdict["reasons"])


def test_legitimate_brand_domain_is_not_flagged_as_impersonation():
    verdict = junk_filter.score_message(
        '"Apple" <no_reply@email.apple.com>', "Your receipt from Apple", "Thanks for your purchase.",
    )
    assert not any("claims 'apple'" in r for r in verdict["reasons"])


def test_link_shortener_and_raw_ip_links_add_signal():
    shortener = junk_filter.score_message("a@example.com", "Check this out", "go here: http://bit.ly/abc123")
    raw_ip = junk_filter.score_message("a@example.com", "Check this out", "go here: http://192.168.1.5/login")
    plain = junk_filter.score_message("a@example.com", "Check this out", "go here: https://example.com/page")

    assert shortener["score"] > plain["score"]
    assert raw_ip["score"] > plain["score"]


def test_score_message_handles_missing_body_and_blank_sender():
    verdict = junk_filter.score_message("", "Hello")
    assert verdict["score"] >= 0
    assert isinstance(verdict["reasons"], list)
