"""Git/GitHub tools for the dev-team agents: list/read the real repo, branch, write
files, push, open a PR, check its status, and (gated) merge it. Read tools exist so an
employee never has to guess at the actual tech stack, layout, or what's already built --
a real gap found live: an employee with only write tools invented a Node/Postgres stack
from nothing and stubbed a real, already-working integration, because it had no way to
look at the repo it was supposedly contributing to.

Local repo operations (clone/fetch/worktree/commit/push) shell out to the real `git`
binary with a fixed argv list per call -- never a shell string, same discipline
claude_cli.py already uses for its own subprocess calls. PR create/status/merge go
straight to GitHub's REST API via httpx rather than depending on the `gh` CLI being
installed wherever Jarvis runs.

One persistent local clone (`_main`) acts as the hub; each branch gets its own
`git worktree` checkout under the workspace root, so concurrent branches never collide
and nothing needs re-cloning per job -- the gpu_bridge.py `generated_media_path` pattern
(a config-defined root, job/branch-namespaced subpaths) applied to code instead of media.

The GitHub PAT is injected here, per call, as an `-c http.extraheader` override rather
than baked into the clone's `.git/config` or ever appearing in a tool argument the model
supplies -- same "credentials never reach conversation history" principle as CCXT's
credential-injecting wrapper.
"""
import base64
import json
import subprocess
from pathlib import Path

import httpx

from .github_client import summarize_checks

GITHUB_API = "https://api.github.com"


class GitOpsError(Exception):
    pass


def _run_git(args: list[str], cwd: Path, token: str | None = None) -> str:
    cmd = ["git"]
    if token:
        # GitHub's git-over-HTTPS endpoint wants Basic auth (any username, the PAT as
        # password -- "x-access-token" is GitHub's own documented convention), NOT the
        # Bearer scheme the REST API uses. Scoped to this one invocation only via -c,
        # never written to any .git/config file.
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        cmd += ["-c", f"http.extraheader=Authorization: Basic {basic}"]
    cmd += args
    result = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    if result.returncode != 0:
        raise GitOpsError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _safe_join(root: Path, rel_path: str) -> Path:
    """Resolves rel_path under root, refusing anything that would escape it (../, an
    absolute path) -- same containment check review.py already applies before serving
    a generated-media file back, applied here before ever writing one."""
    candidate = (root / rel_path).resolve()
    root_resolved = root.resolve()
    if root_resolved not in candidate.parents and candidate != root_resolved:
        raise GitOpsError(f"refusing to write outside the workspace: {rel_path!r}")
    return candidate


class GitOpsClient:
    """Executes the git tools. Same call_tool(name, arguments) shape as every other
    integration, so engine.py's dispatch treats it identically."""

    def __init__(
        self, repo: str, token: str, workspace_path: str,
        author_name: str = "Jarvis", author_email: str = "jarvis@localhost",
    ):
        self.repo = repo
        self.token = token
        self.workspace = Path(workspace_path).resolve()
        self.author_name = author_name
        self.author_email = author_email
        self._http = httpx.Client(
            base_url=GITHUB_API,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )

    # -- workspace management -------------------------------------------------

    @property
    def _main_clone(self) -> Path:
        return self.workspace / "_main"

    def _ensure_main_clone(self) -> None:
        main = self._main_clone
        if (main / ".git").exists():
            _run_git(["fetch", "origin"], cwd=main, token=self.token)
            return
        main.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://github.com/{self.repo}.git"
        _run_git(["clone", url, str(main)], cwd=main.parent, token=self.token)
        _run_git(["config", "user.name", self.author_name], cwd=main)
        _run_git(["config", "user.email", self.author_email], cwd=main)

    def _worktree_dir(self, branch_name: str) -> Path:
        return self.workspace / branch_name

    # -- tools ------------------------------------------------------------------

    def _readable_root(self, branch_name: str | None) -> Path:
        """Resolves which checkout to read from: the given branch's worktree, or the
        main clone (fetched fresh first) when none is given -- so 'look at the repo'
        always means the real, current thing, never a stale local copy."""
        if branch_name is None:
            self._ensure_main_clone()
            return self._main_clone
        root = self._worktree_dir(branch_name)
        if not root.exists():
            raise GitOpsError(f"no such branch checkout {branch_name!r} -- call create_branch first")
        return root

    def list_files(self, path: str = "", branch_name: str | None = None) -> dict:
        root = self._readable_root(branch_name)
        target = _safe_join(root, path or ".")
        if not target.is_dir():
            raise GitOpsError(f"not a directory: {path!r}")
        entries = sorted(
            (f"{p.name}/" if p.is_dir() else p.name) for p in target.iterdir() if p.name != ".git"
        )
        return {"ok": True, "path": path or ".", "entries": entries}

    def read_file(self, path: str, branch_name: str | None = None) -> dict:
        root = self._readable_root(branch_name)
        target = _safe_join(root, path)
        if not target.is_file():
            raise GitOpsError(f"no such file: {path!r}")
        try:
            # utf-8-sig strips a leading BOM rather than returning it as a literal
            # U+FEFF character mixed into the content -- several files in this repo
            # carry one, and it has no business appearing in what a model reads back.
            content = target.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            return {"ok": False, "error": "binary file, cannot display as text"}
        return {"ok": True, "path": path, "content": content}

    def create_branch(self, branch_name: str, base_branch: str = "main") -> dict:
        self._ensure_main_clone()
        worktree_dir = self._worktree_dir(branch_name)

        # branch_name may already exist on origin with real history of its own (e.g. an
        # in-progress PR branch a previous run started, or is continuing). Forking a
        # fresh branch of the same name off base_branch in that case -- which is what
        # this used to always do -- silently orphans that existing work, and reusing a
        # leftover local worktree as-is risks handing back content that's since drifted
        # from the real remote branch (a prior run's abandoned, never-pushed attempt).
        # Both cases must defer to the actual current state of origin/branch_name.
        remote_ref = f"origin/{branch_name}"
        try:
            _run_git(["rev-parse", "--verify", remote_ref], cwd=self._main_clone)
            remote_exists = True
        except GitOpsError:
            remote_exists = False

        if worktree_dir.exists():
            if not remote_exists:
                return {"ok": True, "branch": branch_name,
                        "message": "branch/worktree already exists (local-only, not on origin)"}
            _run_git(["checkout", branch_name], cwd=worktree_dir)
            _run_git(["reset", "--hard", remote_ref], cwd=worktree_dir)
            return {"ok": True, "branch": branch_name,
                    "message": f"branch/worktree already existed; reset to current {remote_ref}"}

        worktree_dir.parent.mkdir(parents=True, exist_ok=True)
        if remote_exists:
            _run_git(["worktree", "add", str(worktree_dir), branch_name],
                      cwd=self._main_clone, token=self.token)
            return {"ok": True, "branch": branch_name, "message": f"checked out existing branch from {remote_ref}"}
        _run_git(
            ["worktree", "add", str(worktree_dir), "-b", branch_name, f"origin/{base_branch}"],
            cwd=self._main_clone, token=self.token,
        )
        return {"ok": True, "branch": branch_name, "base_branch": base_branch}

    def commit_and_push(
        self, branch_name: str, files: list[dict], commit_message: str,
        delete_paths: list[str] | None = None,
    ) -> dict:
        worktree_dir = self._worktree_dir(branch_name)
        if not worktree_dir.exists():
            raise GitOpsError(f"no such branch checkout {branch_name!r} -- call create_branch first")

        for f in files:
            target = _safe_join(worktree_dir, f["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f["content"], encoding="utf-8")

        for rel_path in delete_paths or []:
            target = _safe_join(worktree_dir, rel_path)
            if target.exists():
                target.unlink()

        _run_git(["add", "-A"], cwd=worktree_dir)
        status = _run_git(["status", "--porcelain"], cwd=worktree_dir)
        if not status:
            return {"ok": False, "error": "no changes to commit"}
        _run_git(["commit", "-m", commit_message], cwd=worktree_dir)
        _run_git(["push", "-u", "origin", branch_name], cwd=worktree_dir, token=self.token)
        sha = _run_git(["rev-parse", "HEAD"], cwd=worktree_dir)
        return {"ok": True, "branch": branch_name, "commit_sha": sha, "files_changed": len(files) + len(delete_paths or [])}

    def open_pr(self, branch_name: str, title: str, body: str, base_branch: str = "main") -> dict:
        resp = self._http.post(f"/repos/{self.repo}/pulls", json={
            "title": title, "body": body, "head": branch_name, "base": base_branch,
        })
        if resp.status_code >= 400:
            return {"ok": False, "error": resp.text[:500]}
        data = resp.json()
        return {"ok": True, "pr_number": data["number"], "url": data["html_url"], "state": data["state"]}

    def get_pr_status(self, pr_number: int) -> dict:
        resp = self._http.get(f"/repos/{self.repo}/pulls/{pr_number}")
        if resp.status_code >= 400:
            return {"ok": False, "error": resp.text[:500]}
        pr = resp.json()
        checks_resp = self._http.get(f"/repos/{self.repo}/commits/{pr['head']['sha']}/check-runs")
        checks = checks_resp.json().get("check_runs", []) if checks_resp.status_code < 400 else []
        return {
            "ok": True, "state": pr["state"], "mergeable": pr.get("mergeable"),
            "merged": pr["merged"], "url": pr["html_url"],
            # Real incident: without these, an employee told "PR #24's CI is failing,
            # fix it" had no way to discover what branch that PR actually points at --
            # git_read_file/git_commit_and_push both require a branch_name, and nothing
            # else in this tool set ever surfaces one for an existing PR by number. The
            # employee reported this back as "no branch access", which is exactly what
            # it looked like from where they were sitting.
            "branch_name": pr["head"]["ref"], "base_branch": pr["base"]["ref"],
            "checks": [{"name": c["name"], "status": c["status"], "conclusion": c["conclusion"]} for c in checks],
        }

    def get_pr_files(self, pr_number: int) -> list[dict]:
        """A PR's file-level diff stats -- what check_diff_scope's heuristic and LLM
        judgment both work from. Deliberately drops GitHub's own `patch` text (the
        actual line-by-line diff): file name, status, and +/- counts are what scope
        drift looks like, and dropping patch keeps this cheap regardless of how large a
        change actually is.
        """
        resp = self._http.get(f"/repos/{self.repo}/pulls/{pr_number}/files?per_page=100")
        if resp.status_code >= 400:
            return []
        return [
            {"filename": f["filename"], "status": f["status"],
             "additions": f.get("additions", 0), "deletions": f.get("deletions", 0),
             "changes": f.get("changes", 0)}
            for f in resp.json()
        ]

    def list_open_prs(self) -> list[dict]:
        """Every currently-open PR, for github_client.py's watchdog poll to diff against
        what it last saw. Not exposed as a chat tool -- internal to the watchdog, same as
        _run_git is internal to the tools built on top of it. The query string is
        embedded in the path rather than passed as a separate params= kwarg so this
        works against the same minimal fake HTTP client (get(path) only) the rest of
        this module's tests already use.
        """
        resp = self._http.get(f"/repos/{self.repo}/pulls?state=open&per_page=100")
        if resp.status_code >= 400:
            return []
        return [{"number": p["number"], "title": p["title"], "url": p["html_url"]} for p in resp.json()]

    def merge_pr(self, pr_number: int, merge_method: str = "squash") -> dict:
        """Merges a PR -- but only after re-verifying its real, current state.

        A real incident (PR #19, 2026-09-08) merged with a failing backend-tests
        check and had to be manually reverted the next day; the pattern then
        repeated twice more (PR #21/#22) needing emergency post-merge fix commits.
        The gate that would have caught all three lives here, in the one place
        every merge path funnels through -- engine.py's auto-merge heuristic
        (_try_auto_merge_pr) and the human "confirm" flow both end up calling this
        method, and neither used to re-check anything at the moment of execution.
        Checking here, rather than only at each call site, means no future caller
        can accidentally skip it.

        Requires CI to be unanimously green AND GitHub's own `mergeable` flag to be
        true -- that flag is what "needs a rebase" cashes out to technically
        (GitHub sets it false/null when the branch can't cleanly combine with the
        current base), and nothing previously read it at all.
        """
        status = self.get_pr_status(pr_number)
        if not status.get("ok"):
            return {"ok": False,
                    "error": f"refusing to merge PR #{pr_number}: could not verify its current "
                             f"status first ({status.get('error')})"}
        checks = status.get("checks") or []
        if not checks or any(c.get("conclusion") != "success" for c in checks):
            return {"ok": False,
                    "error": f"refusing to merge PR #{pr_number}: CI is not 100% green "
                             f"({summarize_checks(checks)}). Fix the failing check(s) first."}
        if status.get("mergeable") is not True:
            return {"ok": False,
                    "error": f"refusing to merge PR #{pr_number}: GitHub reports it is not "
                             f"cleanly mergeable (mergeable={status.get('mergeable')!r}) -- the "
                             "branch likely needs a rebase onto the current base branch first."}

        resp = self._http.put(f"/repos/{self.repo}/pulls/{pr_number}/merge", json={"merge_method": merge_method})
        if resp.status_code >= 400:
            return {"ok": False, "error": resp.text[:500]}
        return {"ok": True, **resp.json()}

    # -- dispatch -----------------------------------------------------------------

    def call_tool(self, name: str, arguments: dict) -> dict:
        try:
            if name == "git_list_files":
                return self.list_files(arguments.get("path", ""), arguments.get("branch_name"))
            if name == "git_read_file":
                return self.read_file(arguments["path"], arguments.get("branch_name"))
            if name == "git_create_branch":
                return self.create_branch(arguments["branch_name"], arguments.get("base_branch", "main"))
            if name == "git_commit_and_push":
                return self.commit_and_push(
                    arguments["branch_name"], arguments.get("files", []), arguments["commit_message"],
                    arguments.get("delete_paths"))
            if name == "git_open_pr":
                return self.open_pr(
                    arguments["branch_name"], arguments["title"], arguments.get("body", ""),
                    arguments.get("base_branch", "main"))
            if name == "git_get_pr_status":
                return self.get_pr_status(arguments["pr_number"])
            if name == "git_merge_pr":
                return self.merge_pr(arguments["pr_number"], arguments.get("merge_method", "squash"))
            return {"error": f"unknown git tool {name}"}
        except GitOpsError as e:
            return {"ok": False, "error": str(e)}


# -- diff-scope safety check ------------------------------------------------------
#
# The automated substitute for what manual PR review has actually been catching --
# not a hypothetical: three separate real instances of `main` silently losing
# already-shipped code happened in one day (a stale-branch merge, a "clean" rebase with
# no conflict markers, and a well-intentioned "restoration" PR that quietly rebuilt two
# files as a simpler, broken reimplementation), none of them caught by green CI. This
# is the signal that caught all three after the fact -- large deletions relative to a
# task's stated scope -- built into a check that runs *before* a merge instead of after
# one, feeding Phase 5's auto-merge governance. Fails closed throughout: anything that
# can't be fetched or judged counts as unsafe, never as a silent pass.

SCOPE_JUDGMENT_INSTRUCTIONS = """You are reviewing a pull request's file-level diff for SCOPE, not code quality: does the set of changed files, and the shape of each change, plausibly match ONLY what the task below asked for? You are not judging whether the code is good -- only whether something outside the task's stated scope appears to have been touched, simplified, or removed.

Task assigned:
{task}

Files changed:
{file_list}

Reply with exactly one line of JSON and nothing else, in this form:
{{"safe": true or false, "concerns": ["short concern", ...]}}

Set safe to false if: a file looks unrelated to the task; a large deletion appears in a file the task never asked you to touch; the pattern looks like a rewrite/simplification of something that was probably already working rather than the task's own change; or you are genuinely unsure. An empty concerns list only when safe is true."""


def _heuristic_concerns(files: list[dict]) -> list[str]:
    """Cheap, model-free red flags -- exactly the tell that caught all three real
    incidents referenced above: deletions heavily outweighing additions, or a file
    disappearing entirely."""
    concerns = []
    for f in files:
        name = f.get("filename", "?")
        additions = f.get("additions", 0) or 0
        deletions = f.get("deletions", 0) or 0
        if f.get("status") == "removed":
            concerns.append(f"{name} was deleted entirely ({deletions} line(s))")
        elif deletions >= 10 and deletions > additions * 2:
            concerns.append(
                f"{name}: {deletions} deletions vs {additions} additions -- "
                "large net removal relative to what was added")
    return concerns


def _parse_scope_verdict(output: str | None) -> dict:
    """Same tolerant trailing-JSON-line extraction as staff.parse_verdict, but fails
    closed toward UNSAFE where that one fails closed toward no-alert: an unparseable or
    missing judgment must fall back to manual review, never silently pass a diff as
    clean just because the model's output didn't parse."""
    if not output:
        return {"safe": False, "concerns": ["scope judgment produced no output"]}
    for line in reversed([l.strip().strip("`") for l in output.strip().splitlines()]):
        if not line.startswith("{") or not line.endswith("}"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if "safe" in data:
            concerns = data.get("concerns") or []
            if not isinstance(concerns, list):
                concerns = [str(concerns)]
            return {"safe": bool(data["safe"]), "concerns": [str(c) for c in concerns]}
    return {"safe": False, "concerns": ["scope judgment could not be parsed"]}


def _llm_scope_judgment(llm, task_description: str, files: list[dict], timeout: int = 120) -> dict:
    file_list = "\n".join(
        f"- {f.get('filename')} ({f.get('status')}): +{f.get('additions', 0)}/-{f.get('deletions', 0)}"
        for f in files
    ) or "(no files changed)"
    prompt = SCOPE_JUDGMENT_INSTRUCTIONS.format(task=task_description, file_list=file_list)
    try:
        output = llm.research(prompt, system_prompt="You are a careful, conservative code reviewer.",
                               timeout=timeout)
    except Exception as e:
        return {"safe": False, "concerns": [f"scope judgment call failed: {type(e).__name__}: {e}"]}
    return _parse_scope_verdict(output)


def check_diff_scope(git_ops_client, llm, pr_number: int, task_description: str, timeout: int = 120) -> dict:
    """Does PR #pr_number's actual diff plausibly match what it was assigned to do?

    Two independent signals, combined -- either one raising a concern is enough to call
    this unsafe: a cheap heuristic needing no model call, and an LLM judgment of whether
    the real file list/shape matches the stated task (same "gather candidates, let the
    model decide" pattern used everywhere else in this codebase for fuzzy judgment,
    rather than a hardcoded rule that can't account for a task that genuinely does need
    a large deletion). Returns concrete, readable concerns, not just a bool, so a
    fallback to manual review can tell the owner why, not just "flagged".
    """
    files = git_ops_client.get_pr_files(pr_number)
    if not files:
        # Could genuinely mean "no files changed" or "the fetch failed" -- either way,
        # nothing to safely judge, so this cannot pass as safe.
        return {"safe": False, "concerns": [f"could not fetch a file diff for PR #{pr_number}"], "files": []}

    concerns = list(_heuristic_concerns(files))
    judgment = _llm_scope_judgment(llm, task_description, files, timeout=timeout)
    if not judgment["safe"]:
        concerns.extend(c for c in judgment["concerns"] if c not in concerns)

    return {"safe": not concerns, "concerns": concerns, "files": files}
