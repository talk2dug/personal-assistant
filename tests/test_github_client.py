"""github_client.py's poll-and-diff logic (refresh/summarize_checks) -- tested against a
fake GitOpsClient stand-in rather than real GitHub, since list_open_prs/get_pr_status
are themselves already covered against a fake httpx client in test_git_ops.py. This only
checks that a real state transition is detected and a first-ever-seen PR is not
reported as one, mirroring test_market_data.py's own had_any-style guard tests.
"""
import pytest

from assistant.core import db, github_client


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    github_client.init_github_db(path)
    return path


class FakeGitOps:
    def __init__(self, open_prs, statuses):
        """statuses: {pr_number: status_dict} as GitOpsClient.get_pr_status returns."""
        self._open_prs = open_prs
        self._statuses = statuses

    def list_open_prs(self):
        return self._open_prs

    def get_pr_status(self, pr_number):
        return self._statuses.get(pr_number, {"ok": False, "error": "not found"})


def _status(state="open", mergeable=True, merged=False, checks=None, url="https://github.com/o/r/pull/1"):
    return {"ok": True, "state": state, "mergeable": mergeable, "merged": merged,
            "url": url, "checks": checks or [{"name": "backend", "status": "completed", "conclusion": "success"}]}


def test_summarize_checks_empty_is_none():
    assert github_client.summarize_checks([]) == "none"


def test_summarize_checks_all_success():
    checks = [{"name": "a", "status": "completed", "conclusion": "success"},
              {"name": "b", "status": "completed", "conclusion": "success"}]
    assert github_client.summarize_checks(checks) == "success"


def test_summarize_checks_any_failure_wins_over_pending():
    checks = [{"name": "a", "status": "completed", "conclusion": "failure"},
              {"name": "b", "status": "in_progress", "conclusion": None}]
    assert github_client.summarize_checks(checks) == "failure"


def test_summarize_checks_incomplete_is_pending():
    checks = [{"name": "a", "status": "in_progress", "conclusion": None}]
    assert github_client.summarize_checks(checks) == "pending"


def test_refresh_first_time_seeing_a_pr_is_not_reported_as_a_change(db_path):
    git_ops = FakeGitOps(
        open_prs=[{"number": 1, "title": "Add feature", "url": "https://github.com/o/r/pull/1"}],
        statuses={1: _status()},
    )

    result = github_client.refresh(db_path, git_ops)

    assert result["ok"] is True
    assert result["changes"] == []


def test_refresh_detects_a_check_flipping_to_failure(db_path):
    open_prs = [{"number": 1, "title": "Add feature", "url": "https://github.com/o/r/pull/1"}]
    passing = FakeGitOps(open_prs, {1: _status(checks=[
        {"name": "backend", "status": "completed", "conclusion": "success"}])})
    github_client.refresh(db_path, passing)

    failing = FakeGitOps(open_prs, {1: _status(checks=[
        {"name": "backend", "status": "completed", "conclusion": "failure"}])})
    result = github_client.refresh(db_path, failing)

    assert len(result["changes"]) == 1
    change = result["changes"][0]
    assert change["pr_number"] == 1
    assert change["previous"]["checks_conclusion"] == "success"
    assert change["now"]["checks_conclusion"] == "failure"


def test_refresh_is_quiet_on_a_second_poll_with_nothing_new(db_path):
    open_prs = [{"number": 1, "title": "Add feature", "url": "https://github.com/o/r/pull/1"}]
    git_ops = FakeGitOps(open_prs, {1: _status()})
    github_client.refresh(db_path, git_ops)

    result = github_client.refresh(db_path, git_ops)

    assert result["changes"] == []


def test_refresh_detects_a_pr_merging_after_dropping_out_of_the_open_list(db_path):
    """list_open_prs alone would never see this -- a merged PR just isn't "open" anymore,
    so the watchdog has to specifically re-check anything that used to be open."""
    open_prs = [{"number": 1, "title": "Add feature", "url": "https://github.com/o/r/pull/1"}]
    still_open = FakeGitOps(open_prs, {1: _status(state="open", merged=False)})
    github_client.refresh(db_path, still_open)

    now_merged = FakeGitOps([], {1: _status(state="closed", merged=True, mergeable=None)})
    result = github_client.refresh(db_path, now_merged)

    assert len(result["changes"]) == 1
    change = result["changes"][0]
    assert change["previous"]["state"] == "open"
    assert change["now"]["state"] == "closed"
    assert change["now"]["merged"] is True
    # The title survives even though the merged PR no longer appears in list_open_prs.
    assert change["title"] == "Add feature"


def test_refresh_detects_mergeable_flipping_to_false(db_path):
    open_prs = [{"number": 1, "title": "Add feature", "url": "https://github.com/o/r/pull/1"}]
    clean = FakeGitOps(open_prs, {1: _status(mergeable=True)})
    github_client.refresh(db_path, clean)

    conflicted = FakeGitOps(open_prs, {1: _status(mergeable=False)})
    result = github_client.refresh(db_path, conflicted)

    assert len(result["changes"]) == 1
    # previous["mergeable"] round-trips through sqlite as 1/0, not a real bool -- == is
    # the correct check here, not `is`.
    assert result["changes"][0]["previous"]["mergeable"] == True  # noqa: E712
    assert result["changes"][0]["now"]["mergeable"] is False


def test_refresh_skips_a_pr_whose_status_lookup_failed(db_path):
    git_ops = FakeGitOps(
        open_prs=[{"number": 1, "title": "Add feature", "url": "https://github.com/o/r/pull/1"}],
        statuses={},  # get_pr_status returns {"ok": False, ...} for unknown numbers
    )

    result = github_client.refresh(db_path, git_ops)

    assert result["ok"] is True
    assert result["changes"] == []
