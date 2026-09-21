"""Matching one account across three bureaus that each spell it differently.

Every case here is drawn from Jack's real reports, because every one of them broke a
plausible-looking rule. The stakes are specific: "reported by Experian but not TransUnion"
is a dispute letter and a 30-day clock, so a match invented out of a naming artefact costs
a real move, and a match missed hides a real one.
"""
import pytest

from assistant.core import credit_match as cm


def line(creditor, bureau, balance=None, kind="other", last4=None):
    return {"creditor": creditor, "bureau": bureau, "balance": balance,
            "kind": kind, "account_last4": last4}


class TestNames:
    @pytest.mark.parametrize("a,b", [
        # TransUnion truncates to a fixed width; Equifax writes the legal entity.
        ("BRIDGECRES", "Bridgecrest Credit Company"),
        ("ONLINE INFORMATIO", "ONLINE INFORMATION SERVI"),
        # Each bureau abbreviates the issuer its own way.
        ("WFBNA CARD", "WELLS FARGO CARD SERV"),
        ("CHIMEFIN/STRIDE BANK", "CHIME/STRIDE BANK NA"),
        # Singular and plural of the same agency.
        ("I C SYSTEM INC", "I C SYSTEMS COLLECTION"),
    ])
    def test_the_same_lender_written_differently_matches(self, a, b):
        assert cm._tokens_match(cm.tokens(a), cm.tokens(b)) is not None

    @pytest.mark.parametrize("a,b", [
        # The one that merged ten rows into a single "account": a common corporate word.
        ("CAPITAL ONE", "JEFFERSON CAPITAL SYSTEMS"),
        ("EXPRESS RECOVERY INC", "CREDIT COLLECTION SERVICES"),
        ("COMENITYBA", "CAPITAL ONE"),
    ])
    def test_different_lenders_do_not_match(self, a, b):
        assert cm._tokens_match(cm.tokens(a), cm.tokens(b)) is None

    def test_a_two_word_brand_stays_one_brand(self):
        """Split into words, "CAPITAL ONE" matches anything else containing "CAPITAL"."""
        assert cm.tokens("CAPITAL ONE") == ["CAPITALONE"]
        assert cm.tokens("JEFFERSON CAPITAL SYSTEMS") == ["JEFFERSONCAPITAL"]

    def test_an_all_generic_name_keeps_its_words(self):
        """"CREDIT COLLECTION SERVICES" is a real company. Dropping every generic word
        would leave nothing and group it with every other all-generic name."""
        assert cm.tokens("CREDIT COLLECTION SERVICES")


class TestKeepingAccountsApart:
    def test_an_auto_loan_is_never_the_same_as_a_credit_card(self):
        """Capital One issues both. Matching on the name alone merged the car loan with
        the cards into one ten-row "account"."""
        rows = [line("CAPITAL ONE AUTO", "transunion", 10998, kind="auto"),
                line("CAPITAL ONE", "experian", 684, kind="credit_card")]
        assert len(cm.group_accounts(rows)) == 2

    def test_a_charge_off_reported_as_a_collection_still_matches(self):
        """One bureau calls it a credit card, another calls it a collection. That
        disagreement is the thing worth disputing -- refusing to match would hide it."""
        rows = [line("WELLS FARGO CARD SERV", "transunion", 4997, kind="credit_card"),
                line("WELLS FARGO CARD SERV", "equifax", 4997, kind="collections")]
        assert len(cm.group_accounts(rows)) == 1

    def test_two_accounts_at_one_agency_stay_separate(self):
        """I C System reports two collections with different balances on each bureau.
        Merged, they would look like one account the bureaus disagree about."""
        rows = [line("I C SYSTEMS COLLECTION", "transunion", 255, kind="collections"),
                line("I C SYSTEMS COLLECTION", "transunion", 229, kind="collections"),
                line("I C SYSTEM INC", "experian", 255, kind="collections"),
                line("I C SYSTEM INC", "experian", 229, kind="collections")]
        groups = cm.group_accounts(rows)
        assert len(groups) == 2
        for group in groups:
            assert sorted(group["bureaus"]) == ["experian", "transunion"]
            assert group["balances_agree"]

    def test_different_last_four_means_different_accounts(self):
        rows = [line("CAPITAL ONE", "equifax", 0, kind="credit_card", last4="6656"),
                line("CAPITAL ONE", "equifax", 10998, kind="credit_card", last4="1001")]
        assert len(cm.group_accounts(rows)) == 2

    def test_one_bureau_cannot_appear_twice_in_one_account(self):
        """A bureau listing something twice is listing two accounts."""
        rows = [line("UPLIFT", "equifax", 100), line("UPLIFT", "equifax", 250)]
        assert len(cm.group_accounts(rows)) == 2


class TestWhatItReports:
    def test_a_group_records_who_reports_it_and_what_they_say(self):
        rows = [line("JPMCB CARD SERVICES", "transunion", 0, kind="credit_card"),
                line("JPMCB CARD", "experian", 21246, kind="credit_card")]
        group = cm.group_accounts(rows)[0]
        assert sorted(group["bureaus"]) == ["experian", "transunion"]
        assert group["balances_agree"] is False
        assert group["balances"]["experian"] == 21246

    def test_agreement_is_reported_when_they_agree(self):
        rows = [line("MISSION LANE TAB BANK", "transunion", 0, kind="credit_card"),
                line("MISSION LANE TAB BANK", "experian", 0, kind="credit_card")]
        assert cm.group_accounts(rows)[0]["balances_agree"] is True

    def test_confidence_is_reported_honestly(self):
        """A caller must be able to tell a certain match from a guess, because only one of
        them is worth a dispute letter on its own."""
        exact = cm.group_accounts([line("MISSION LANE", "transunion", 0),
                                   line("MISSION LANE", "experian", 0)])[0]
        truncated = cm.group_accounts([line("ONLINE INFORMATIO", "transunion", 1404),
                                       line("ONLINE INFORMATION SERVI", "experian", 1404)])[0]
        assert exact["confidence"] == "exact"
        assert truncated["confidence"] == "prefix"

    def test_an_account_only_one_bureau_reports_is_marked_single(self):
        groups = cm.group_accounts([line("COMENITYBA", "transunion", 1405)])
        assert groups[0]["confidence"] == "single" and groups[0]["bureaus"] == ["transunion"]

    def test_grouping_nothing_is_not_an_error(self):
        assert cm.group_accounts([]) == []
