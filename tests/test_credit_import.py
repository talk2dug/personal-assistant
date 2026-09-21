"""Getting credit reports in, and measuring what moved between them.

Three things are worth defending here, and none of them is "the regex is clever":

  1. **Nothing sensitive is retained.** A credit report carries an SSN and full account
     numbers. Anything that could end up in a log, an error message or the database gets
     redacted first.
  2. **A failed parse must not look like a clean report.** That distinction is the whole
     difference between "we could not read your accounts" and "you have no debts", and
     only one of those is good news.
  3. **Comparison is the point of re-uploading.** A single report is a snapshot; the
     question he actually asked is whether the repair work is doing anything.

All fixtures are synthetic. No real report, real name or real number appears in this file.
"""
import pytest

from assistant.core import credit_import as ci, db as core_db, personal_db


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "credit.db")
    core_db.init_db(path)
    personal_db.init_personal_db(path)
    core_db.upsert_user(path, "111", "Dug", "owner")
    return path


@pytest.fixture
def owner(db):
    return core_db.get_user_by_chat_id(db, "111")["id"]


REPORT = """TransUnion Credit Report
Prepared for: A Person
Social Security Number: 123-45-6789
VantageScore 3.0: 642

CAPITAL ONE BANK
Account Number: ****5678
Account Type: Credit Card
Status: Open
Balance: $1,240.00
Credit Limit: $3,000.00
Date Opened: 03/14/2019

MIDLAND CREDIT MANAGEMENT
Account Number: ****9012
Account Type: Collection
Status: In Collections
Balance: $842.50
Past Due: $842.50
Date Opened: 07/02/2022
"""

NEW_ACCOUNT_BLOCK = """
NEW BANK USA
Account Number: ****4444
Account Type: Credit Card
Status: Open
Balance: $50.00
Credit Limit: $500.00
"""

MYSTERY_REPORT = """SOME LENDER
Account Number: ****1111
Status: Open
Balance: $10.00
"""


def write_and_import(db, owner, tmp_path, text, name="report.txt", pulled_on=None):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return ci.import_file(db, owner, str(path), pulled_on=pulled_on)


class TestRedaction:
    def test_an_ssn_never_survives(self):
        assert "123-45-6789" not in ci.redact("SSN: 123-45-6789")
        assert "[SSN REDACTED]" in ci.redact("SSN: 123-45-6789")

    def test_a_full_account_number_is_cut_to_its_last_four(self):
        out = ci.redact("Account 4147202512345678 is open")
        assert "4147202512345678" not in out and "5678" in out

    def test_redaction_is_blunt_on_purpose(self):
        """Better to redact a harmless long number than to let one real account number
        through because the pattern was clever."""
        assert "1234567890123" not in ci.redact("reference 1234567890123")

    def test_short_numbers_are_left_alone(self):
        assert ci.redact("balance $1,240.00 opened 2019") == "balance $1,240.00 opened 2019"

    def test_empty_input_is_safe(self):
        assert ci.redact(None) == "" and ci.redact("") == ""


class TestParsing:
    def test_the_bureau_is_recognised(self):
        assert ci.detect_bureau(REPORT) == "transunion"

    def test_the_score_is_read_when_stated(self):
        assert ci.detect_score(REPORT) == 642

    @pytest.mark.parametrize("text", [
        "Credit Limit: $750",
        "Credit Card ending 1234 Limit 750",
        "High Balance 750",
        "Revolving credit account 750 dollars",
        "Total accounts 750",
        "Payment amount 450",
        "Score 999",
    ])
    def test_a_number_that_is_not_a_score_is_never_read_as_one(self, text):
        """The bug this exists for: the bare word "credit" used to trigger detection, and
        in a CREDIT report that word sits beside every limit on every page. A real upload
        came back claiming a score of 750 that was actually a $750 credit limit -- stored
        as fact, and every dispute and payoff decision would have been steered by it.

        An unfound score is a blank field he can fill in. An invented one is silent and
        wrong, so this errs toward None in every direction.
        """
        assert ci.detect_score(text) is None

    @pytest.mark.parametrize("text,expected", [
        ("Your FICO Score 8 is 712", 712),
        ("VantageScore 3.0: 642", 642),
        ("Credit Score: 688", 688),
    ])
    def test_a_real_score_is_still_read(self, text, expected):
        assert ci.detect_score(text) == expected

    def test_a_year_is_not_mistaken_for_a_score(self):
        """A stray number in the right range would otherwise become his credit score."""
        assert ci.detect_score("Report generated 2026. No score included.") is None

    def test_accounts_come_out_with_their_numbers(self):
        lines = ci.parse_tradelines(REPORT)["tradelines"]
        card = next(t for t in lines if "CAPITAL" in t["creditor"].upper())
        assert card["balance"] == 1240.0
        assert card["credit_limit"] == 3000.0
        assert card["account_last4"] == "5678"
        assert card["kind"] == "credit_card"

    def test_a_collection_is_classified_as_one(self):
        lines = ci.parse_tradelines(REPORT)["tradelines"]
        collection = next(t for t in lines if "MIDLAND" in t["creditor"].upper())
        assert collection["kind"] == "collections"
        assert collection["past_due"] == 842.5

    def test_report_furniture_is_not_turned_into_an_account(self):
        """A real upload produced a tradeline whose creditor was "Your TransUnion Credit
        Report Personal", with a limit and a status invented from nearby numbers. An
        imaginary account is worse than a missing one: it can be disputed, reasoned about
        and paid."""
        header = ("Your TransUnion Credit Report Personal Information "
                  "Account number ending 4677 Credit Limit $608 Status open")
        result = ci.parse_tradelines(header)
        assert result["tradelines"] == []
        assert result["unparsed"], "and it is counted, not silently dropped"

    def test_a_block_with_no_money_at_all_is_not_an_account(self):
        """A heading that happened to sit near a number is not a tradeline."""
        block = "SOME BANK Account opened 01/01/2020"
        assert ci.parse_tradelines(block)["tradelines"] == []

    def test_no_full_account_number_is_ever_stored(self):
        text = REPORT.replace("****5678", "4147202512345678")
        for line in ci.parse_tradelines(text)["tradelines"]:
            assert "4147202512345678" not in str(line)

    def test_unreadable_text_is_reported_not_swallowed(self):
        """The distinction that matters: 'we could not read this' must never render as
        'you have no accounts'."""
        result = ci.parse_tradelines("")
        assert result["tradelines"] == [] and result["unparsed"]

    def test_an_unsupported_format_says_so(self, tmp_path):
        bad = tmp_path / "report.docx"
        bad.write_text("x")
        with pytest.raises(ValueError, match="unsupported"):
            ci.extract_text(str(bad))


class TestStoring:
    def test_a_report_and_its_accounts_are_stored(self, db, owner, tmp_path):
        result = write_and_import(db, owner, tmp_path, REPORT)
        assert result["bureau"] == "transunion"
        assert result["score"] == 642
        assert result["tradelines_stored"] >= 2

    def test_the_specialist_can_now_see_them(self, db, owner, tmp_path):
        """The whole point: credit.py's briefing said NO CREDIT REPORT UPLOADED YET."""
        from assistant.core import credit

        write_and_import(db, owner, tmp_path, REPORT)
        report = credit.latest_report(db, owner)
        assert report is not None and len(report["tradelines"]) >= 2

    def test_utilisation_becomes_real_instead_of_guesswork(self, db, owner, tmp_path):
        from assistant.core import credit

        write_and_import(db, owner, tmp_path, REPORT)
        report = credit.latest_report(db, owner)
        assert credit.utilisation(report["tradelines"]), (
            "utilisation needs limits, which is exactly why they are parsed")

    def test_an_unknown_bureau_becomes_other_rather_than_failing(self, db, owner, tmp_path):
        """A stored report with the wrong label is worth far more than no report."""
        result = write_and_import(db, owner, tmp_path, MYSTERY_REPORT, name="mystery.txt")
        assert result["bureau"] == "other"

    def test_an_empty_file_is_stored_as_unreadable_not_as_clean(self, db, owner, tmp_path):
        result = write_and_import(db, owner, tmp_path, "   ", name="blank.txt")
        assert result["tradelines_stored"] == 0 and result["unparsed"]


class TestAnEmptyImportIsNotStored:
    """credit.latest_report picks the newest row, so an unreadable upload would silently
    become "the current picture" and shadow every account already parsed from the other
    bureaus -- the specialist would be told the file is clean. It happened with a scanned
    Experian PDF while twenty-eight real accounts sat behind it."""

    def test_a_report_with_no_accounts_is_not_written(self, db, owner, tmp_path):
        result = write_and_import(db, owner, tmp_path, "   ", name="scanned.txt")
        assert result["stored"] is False and result["report_id"] is None
        assert result["diagnosis"]

    def test_it_cannot_shadow_a_good_report(self, db, owner, tmp_path):
        from assistant.core import credit

        write_and_import(db, owner, tmp_path, REPORT, "good.txt", "2026-01-01")
        write_and_import(db, owner, tmp_path, "   ", "scanned.txt", "2026-02-01")
        latest = credit.latest_report(db, owner)
        assert latest is not None and len(latest["tradelines"]) >= 2, (
            "the unreadable upload must not become the current picture")

    def test_it_can_be_forced_when_a_caller_really_wants_the_record(self, db, owner, tmp_path):
        path = tmp_path / "empty.txt"
        path.write_text("   ", encoding="utf-8")
        result = ci.import_file(db, owner, str(path), store_empty=True)
        assert result["stored"] is True and result["report_id"]


class TestProgress:
    def test_one_report_cannot_be_compared_and_says_so(self, db, owner, tmp_path):
        write_and_import(db, owner, tmp_path, REPORT, "a.txt", "2026-01-01")
        result = ci.compare(db, owner)
        assert result["comparable"] is False and "another" in result["reason"]

    def test_the_diff_shows_what_actually_moved(self, db, owner, tmp_path):
        later = REPORT.replace("Balance: $1,240.00", "Balance: $828.00")
        later = later.replace("VantageScore 3.0: 642", "VantageScore 3.0: 667")
        write_and_import(db, owner, tmp_path, REPORT, "jan.txt", "2026-01-01")
        write_and_import(db, owner, tmp_path, later, "feb.txt", "2026-02-01")

        result = ci.compare(db, owner)
        assert result["comparable"] is True
        assert result["score_delta"] == 25
        moved = next(c for c in result["accounts_changed"]
                     if "CAPITAL" in (c["creditor"] or "").upper())
        assert moved["balance_delta"] == -412.0

    def test_an_account_that_falls_off_is_noticed(self, db, owner, tmp_path):
        """A collection disappearing is the best news a repair effort can produce, so it
        cannot be left for him to spot by eye."""
        without = REPORT.split("MIDLAND")[0]
        write_and_import(db, owner, tmp_path, REPORT, "jan.txt", "2026-01-01")
        write_and_import(db, owner, tmp_path, without, "feb.txt", "2026-02-01")
        gone = ci.compare(db, owner)["accounts_gone"]
        assert any("MIDLAND" in (t["creditor"] or "").upper() for t in gone)

    def test_a_new_account_is_noticed(self, db, owner, tmp_path):
        write_and_import(db, owner, tmp_path, REPORT, "jan.txt", "2026-01-01")
        write_and_import(db, owner, tmp_path, REPORT + NEW_ACCOUNT_BLOCK,
                         "feb.txt", "2026-02-01")
        appeared = ci.compare(db, owner)["accounts_appeared"]
        assert any("NEW BANK" in (t["creditor"] or "").upper() for t in appeared)

    def test_an_account_is_tracked_across_reports_despite_a_new_balance(self, db, owner, tmp_path):
        """Creditor plus last four is the identity. A balance-based key would read every
        payment as a different account."""
        later = REPORT.replace("Balance: $1,240.00", "Balance: $1.00")
        write_and_import(db, owner, tmp_path, REPORT, "jan.txt", "2026-01-01")
        write_and_import(db, owner, tmp_path, later, "feb.txt", "2026-02-01")
        result = ci.compare(db, owner)
        assert result["accounts_appeared"] == [] and result["accounts_gone"] == []


class TestWebArchive:
    """Experian's PDF export is images with no text at all, so the report arrived as a
    Safari .webarchive emailed from a phone. That is a binary plist wrapping the page --
    the same HTML the browser showed, which reads perfectly."""

    def _archive(self, tmp_path, html):
        import plistlib

        path = tmp_path / "report.webarchive"
        with open(path, "wb") as handle:
            plistlib.dump({"WebMainResource": {
                "WebResourceData": html.encode("utf-8"),
                "WebResourceMIMEType": "text/html",
                "WebResourceTextEncodingName": "UTF-8"}}, handle)
        return str(path)

    def test_the_page_inside_is_read(self, tmp_path):
        path = self._archive(tmp_path, "<html><body><p>Experian</p><p>Balance</p></body></html>")
        text = ci.extract_text(path)
        assert "Experian" in text and ci.detect_bureau(text) == "experian"

    def test_scripts_and_styles_are_not_treated_as_content(self, tmp_path):
        path = self._archive(
            tmp_path, "<html><head><style>.x{color:red}</style>"
                      "<script>var balance=999</script></head><body>CAPITAL ONE</body></html>")
        text = ci.extract_text(path)
        assert "color:red" not in text and "var balance" not in text
        assert "CAPITAL ONE" in text

    def test_tags_become_line_breaks_not_nothing(self, tmp_path):
        """Accounts live in table cells. Joining cells with no separator runs a value
        straight into the next label, which no label-based parser can read."""
        path = self._archive(tmp_path, "<td>Balance</td><td>$1,240</td><td>Credit Limit</td>")
        text = ci.extract_text(path)
        assert "Balance" in text and "$1,240" in text
        assert "Balance$1,240" not in text.replace(" ", "")

    def test_a_lone_account_name_line_splits_experian_records(self):
        """Experian puts every label on its own line, so that is its record boundary --
        and it must not disturb TransUnion, whose header is "Account Name Account Number"
        on a single line."""
        experian = ("Account Name\nCAPITAL ONE\nBalance\n$100\n"
                    "Account Name\nCHASE\nBalance\n$200\n")
        assert len(ci.split_records(experian)) == 2
