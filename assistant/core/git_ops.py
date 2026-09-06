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
import subprocess
from pathlib import Path

import httpx

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
        if worktree_dir.exists():
            return {"ok": True, "branch": branch_name, "message": "branch/worktree already exists"}
        worktree_dir.parent.mkdir(parents=True, exist_ok=True)
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
            "checks": [{"name": c["name"], "status": c["status"], "conclusion": c["conclusion"]} for c in checks],
        }

    def merge_pr(self, pr_number: int, merge_method: str = "squash") -> dict:
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
