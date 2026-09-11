"""GitOpsClient's local git operations (create_branch/commit_and_push) are exercised
against a real local git repo standing in for "GitHub" -- no network involved, so these
prove the actual subprocess/worktree logic rather than a mocked stand-in for it. The
PR-related methods (open_pr/get_pr_status/merge_pr) are pure GitHub REST calls, tested
with a fake httpx client instead.
"""
import subprocess

import pytest

from assistant.core.git_ops import GitOpsClient, GitOpsError, _safe_join


def _git(args, cwd):
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def bare_remote(tmp_path):
    """A real local bare repo standing in for the GitHub remote, with one commit on main."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(["init", "--bare", "-b", "main"], remote)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(["init", "-b", "main"], seed)
    _git(["config", "user.email", "seed@example.com"], seed)
    _git(["config", "user.name", "Seed"], seed)
    (seed / "README.md").write_text("hello\n")
    _git(["add", "-A"], seed)
    _git(["commit", "-m", "initial"], seed)
    _git(["remote", "add", "origin", str(remote)], seed)
    _git(["push", "origin", "main"], seed)
    return remote


@pytest.fixture
def client(tmp_path, bare_remote):
    workspace = tmp_path / "workspace"
    # repo/token/workspace_path: repo is unused by local git ops (only PR methods hit
    # GitHub), token=None means _run_git never adds the extraheader override, and the
    # "remote" here is a plain local path rather than a real github.com URL.
    c = GitOpsClient(repo="owner/repo", token=None, workspace_path=str(workspace),
                      author_name="Test Bot", author_email="bot@example.com")
    # Point the client at the local bare repo instead of a real GitHub URL.
    c._ensure_main_clone = lambda: _clone_local(c, bare_remote)
    return c


def _clone_local(client: GitOpsClient, remote_path) -> None:
    main = client._main_clone
    if (main / ".git").exists():
        _git(["fetch", "origin"], main)
        return
    main.parent.mkdir(parents=True, exist_ok=True)
    _git(["clone", str(remote_path), str(main)], main.parent)
    _git(["config", "user.name", client.author_name], main)
    _git(["config", "user.email", client.author_email], main)


def test_create_branch_makes_a_real_worktree(client):
    result = client.create_branch("feature/one")
    assert result["ok"] is True
    assert (client.workspace / "feature/one").is_dir()
    assert (client.workspace / "feature/one" / "README.md").exists()


def test_create_branch_is_idempotent(client):
    client.create_branch("feature/one")
    result = client.create_branch("feature/one")
    assert result["ok"] is True
    assert "already exists" in result["message"]


def test_create_branch_checks_out_an_existing_remote_branch_instead_of_forking_fresh(client, bare_remote, tmp_path):
    # Simulate a PR branch that already has real history on origin -- e.g. one an
    # earlier run started, or a human pushed. create_branch used to always fork a new
    # branch of the same name off base_branch here, silently orphaning this content.
    other = tmp_path / "other_clone"
    _git(["clone", str(bare_remote), str(other)], tmp_path)
    _git(["config", "user.email", "other@example.com"], other)
    _git(["config", "user.name", "Other"], other)
    _git(["checkout", "-b", "feature/existing"], other)
    (other / "existing.txt").write_text("real pr content\n")
    _git(["add", "-A"], other)
    _git(["commit", "-m", "existing PR work"], other)
    _git(["push", "origin", "feature/existing"], other)

    result = client.create_branch("feature/existing")
    assert result["ok"] is True
    worktree = client.workspace / "feature/existing"
    assert (worktree / "existing.txt").read_text() == "real pr content\n"


def test_create_branch_resyncs_a_stale_leftover_worktree_to_current_origin(client, bare_remote, tmp_path):
    # First run: the branch is created fresh (doesn't exist on origin yet) and picks up
    # a local commit that's never pushed -- standing in for an earlier, incomplete or
    # abandoned employee attempt.
    client.create_branch("feature/stale")
    worktree = client.workspace / "feature/stale"
    (worktree / "stale_local_only.txt").write_text("abandoned attempt\n")
    _git(["add", "-A"], worktree)
    _git(["commit", "-m", "abandoned local-only work"], worktree)

    # Meanwhile, real work lands on origin for this same branch name from elsewhere.
    other = tmp_path / "other_clone2"
    _git(["clone", str(bare_remote), str(other)], tmp_path)
    _git(["config", "user.email", "other@example.com"], other)
    _git(["config", "user.name", "Other"], other)
    _git(["checkout", "-b", "feature/stale"], other)
    (other / "real.txt").write_text("real current content\n")
    _git(["add", "-A"], other)
    _git(["commit", "-m", "real work"], other)
    _git(["push", "origin", "feature/stale"], other)

    # A fresh run's create_branch call must resync to the real current origin state,
    # not silently hand back the stale leftover as if it were current.
    result = client.create_branch("feature/stale")
    assert result["ok"] is True
    assert "reset to current" in result["message"]
    assert (worktree / "real.txt").exists()
    assert not (worktree / "stale_local_only.txt").exists()


def test_commit_and_push_writes_files_and_pushes_to_the_real_remote(client, bare_remote):
    client.create_branch("feature/two")
    result = client.commit_and_push(
        "feature/two", files=[{"path": "new.txt", "content": "hi\n"}],
        commit_message="add new.txt",
    )
    assert result["ok"] is True
    assert result["files_changed"] == 1

    # Confirm the push actually landed on the "remote" -- clone it fresh and check.
    verify = client.workspace / "_verify"
    _git(["clone", "--branch", "feature/two", str(bare_remote), str(verify)], client.workspace)
    assert (verify / "new.txt").read_text() == "hi\n"


def test_commit_and_push_can_delete_files(client):
    client.create_branch("feature/three")
    client.commit_and_push("feature/three", files=[{"path": "temp.txt", "content": "x"}],
                            commit_message="add temp")
    result = client.commit_and_push("feature/three", files=[], delete_paths=["temp.txt"],
                                     commit_message="remove temp")
    assert result["ok"] is True
    assert not (client.workspace / "feature/three" / "temp.txt").exists()


def test_commit_and_push_with_no_changes_reports_it_rather_than_erroring(client):
    client.create_branch("feature/four")
    result = client.commit_and_push("feature/four", files=[], commit_message="nothing to do")
    assert result["ok"] is False
    assert "no changes" in result["error"]


def test_commit_and_push_requires_an_existing_branch(client):
    with pytest.raises(GitOpsError, match="create_branch first"):
        client.commit_and_push("never-created", files=[{"path": "x.txt", "content": "x"}],
                                commit_message="msg")


def test_list_files_shows_the_real_main_branch_by_default(client):
    result = client.list_files()
    assert result["ok"] is True
    assert "README.md" in result["entries"]


def test_list_files_reads_a_branch_worktree_when_given_one(client):
    client.create_branch("feature/six")
    client.commit_and_push("feature/six", files=[{"path": "new.txt", "content": "hi"}],
                            commit_message="add new.txt")
    result = client.list_files(branch_name="feature/six")
    assert "new.txt" in result["entries"]
    # main itself is untouched -- the write only ever landed on the branch.
    assert "new.txt" not in client.list_files()["entries"]


def test_list_files_requires_an_existing_branch(client):
    with pytest.raises(GitOpsError, match="create_branch first"):
        client.list_files(branch_name="never-created")


def test_read_file_returns_the_real_content(client):
    result = client.read_file("README.md")
    assert result["ok"] is True
    assert result["content"] == "hello\n"


def test_read_file_reports_a_missing_file_rather_than_crashing(client):
    with pytest.raises(GitOpsError, match="no such file"):
        client.read_file("does-not-exist.txt")


def test_call_tool_dispatches_read_tools(client):
    result = client.call_tool("git_list_files", {})
    assert "README.md" in result["entries"]
    result = client.call_tool("git_read_file", {"path": "README.md"})
    assert result["content"] == "hello\n"


def test_safe_join_refuses_path_traversal(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    with pytest.raises(GitOpsError, match="outside the workspace"):
        _safe_join(root, "../../etc/passwd")


def test_safe_join_allows_nested_paths(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    result = _safe_join(root, "src/deep/file.py")
    assert result == (root / "src" / "deep" / "file.py").resolve()


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    @property
    def text(self):
        return str(self._payload)


class FakeHTTP:
    def __init__(self):
        self.calls = []

    def post(self, path, json=None):
        self.calls.append(("POST", path, json))
        return FakeResponse(201, {"number": 42, "html_url": "https://github.com/owner/repo/pull/42", "state": "open"})

    def get(self, path):
        self.calls.append(("GET", path, None))
        if "check-runs" in path:
            return FakeResponse(200, {"check_runs": [{"name": "backend-tests", "status": "completed", "conclusion": "success"}]})
        if path.startswith("/repos/owner/repo/pulls?"):
            return FakeResponse(200, [
                {"number": 42, "title": "My PR", "html_url": "https://github.com/owner/repo/pull/42"},
                {"number": 43, "title": "Another PR", "html_url": "https://github.com/owner/repo/pull/43"},
            ])
        if "/files" in path:
            return FakeResponse(200, [
                {"filename": "assistant/core/thing.py", "status": "modified",
                 "additions": 10, "deletions": 2, "changes": 12, "patch": "@@ ... @@"},
            ])
        return FakeResponse(200, {
            "state": "open", "mergeable": True, "merged": False,
            "html_url": "https://github.com/owner/repo/pull/42", "head": {"sha": "abc123"},
        })

    def put(self, path, json=None):
        self.calls.append(("PUT", path, json))
        return FakeResponse(200, {"merged": True, "sha": "def456"})


def test_open_pr_calls_the_github_api(client):
    client._http = FakeHTTP()
    result = client.open_pr("feature/one", "My PR", "body text")
    assert result == {"ok": True, "pr_number": 42, "url": "https://github.com/owner/repo/pull/42", "state": "open"}
    assert client._http.calls[0][0] == "POST"


def test_get_pr_status_includes_check_runs(client):
    client._http = FakeHTTP()
    result = client.get_pr_status(42)
    assert result["ok"] is True
    assert result["checks"] == [{"name": "backend-tests", "status": "completed", "conclusion": "success"}]


def test_list_open_prs_returns_number_title_and_url(client):
    client._http = FakeHTTP()
    result = client.list_open_prs()
    assert result == [
        {"number": 42, "title": "My PR", "url": "https://github.com/owner/repo/pull/42"},
        {"number": 43, "title": "Another PR", "url": "https://github.com/owner/repo/pull/43"},
    ]
    assert client._http.calls[0][0] == "GET"
    assert client._http.calls[0][1].startswith("/repos/owner/repo/pulls?")


def test_list_open_prs_returns_empty_on_error(client):
    class ErrorHTTP(FakeHTTP):
        def get(self, path):
            self.calls.append(("GET", path, None))
            return FakeResponse(404, {"message": "not found"})

    client._http = ErrorHTTP()
    assert client.list_open_prs() == []


def test_get_pr_files_drops_the_patch_text(client):
    client._http = FakeHTTP()
    result = client.get_pr_files(42)
    assert result == [
        {"filename": "assistant/core/thing.py", "status": "modified",
         "additions": 10, "deletions": 2, "changes": 12},
    ]


def test_get_pr_files_returns_empty_on_error(client):
    class ErrorHTTP(FakeHTTP):
        def get(self, path):
            self.calls.append(("GET", path, None))
            return FakeResponse(404, {"message": "not found"})

    client._http = ErrorHTTP()
    assert client.get_pr_files(42) == []


def test_merge_pr_calls_the_github_api(client):
    client._http = FakeHTTP()
    result = client.merge_pr(42, merge_method="squash")
    assert result == {"ok": True, "merged": True, "sha": "def456"}
    # FakeHTTP's default GET already reports mergeable=True and green checks, so the
    # pre-merge re-verification passes and the PUT still happens -- just no longer first.
    assert ("PUT", "/repos/owner/repo/pulls/42/merge", {"merge_method": "squash"}) in client._http.calls


def test_merge_pr_refuses_when_ci_is_not_green(client):
    class RedCI(FakeHTTP):
        def get(self, path):
            self.calls.append(("GET", path, None))
            if "check-runs" in path:
                return FakeResponse(200, {"check_runs": [
                    {"name": "backend-tests", "status": "completed", "conclusion": "failure"}]})
            return FakeResponse(200, {
                "state": "open", "mergeable": True, "merged": False,
                "html_url": "https://github.com/owner/repo/pull/42", "head": {"sha": "abc123"},
            })

    client._http = RedCI()
    result = client.merge_pr(42)
    assert result["ok"] is False
    assert "not 100% green" in result["error"]
    assert not any(call[0] == "PUT" for call in client._http.calls)


def test_merge_pr_refuses_when_not_cleanly_mergeable(client):
    class Unmergeable(FakeHTTP):
        def get(self, path):
            self.calls.append(("GET", path, None))
            if "check-runs" in path:
                return FakeResponse(200, {"check_runs": [
                    {"name": "backend-tests", "status": "completed", "conclusion": "success"}]})
            return FakeResponse(200, {
                "state": "open", "mergeable": False, "merged": False,
                "html_url": "https://github.com/owner/repo/pull/42", "head": {"sha": "abc123"},
            })

    client._http = Unmergeable()
    result = client.merge_pr(42)
    assert result["ok"] is False
    assert "needs a rebase" in result["error"]
    assert not any(call[0] == "PUT" for call in client._http.calls)


def test_merge_pr_refuses_when_mergeable_still_unknown(client):
    class StillComputing(FakeHTTP):
        def get(self, path):
            self.calls.append(("GET", path, None))
            if "check-runs" in path:
                return FakeResponse(200, {"check_runs": [
                    {"name": "backend-tests", "status": "completed", "conclusion": "success"}]})
            return FakeResponse(200, {
                "state": "open", "mergeable": None, "merged": False,
                "html_url": "https://github.com/owner/repo/pull/42", "head": {"sha": "abc123"},
            })

    client._http = StillComputing()
    result = client.merge_pr(42)
    assert result["ok"] is False
    assert not any(call[0] == "PUT" for call in client._http.calls)


def test_call_tool_dispatches_by_name(client):
    client._http = FakeHTTP()
    client.create_branch("feature/five")
    result = client.call_tool("git_commit_and_push", {
        "branch_name": "feature/five", "files": [{"path": "a.txt", "content": "a"}],
        "commit_message": "msg",
    })
    assert result["ok"] is True

    result = client.call_tool("git_open_pr", {"branch_name": "feature/five", "title": "t"})
    assert result["ok"] is True

    result = client.call_tool("unknown_tool", {})
    assert "error" in result
