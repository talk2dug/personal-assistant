"""Chat-facing tool schemas for git_ops.py — same split as kroger_recipe.py: the schema
list and system note live here, the actual client lives in git_ops.py.
"""

GIT_TOOLS = [
    {"type": "function", "function": {
        "name": "git_list_files",
        "description": (
            "List files under a directory of the real Jarvis repository. Use this before "
            "writing anything -- never assume a tech stack, module layout, or whether an "
            "integration already exists; look first. Defaults to the repo root on main; "
            "pass branch_name to look inside a branch you're already working on."
        ),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory path relative to the repo root. Defaults to the root."},
            "branch_name": {"type": "string", "description": "Defaults to 'main'."},
        }},
    }},
    {"type": "function", "function": {
        "name": "git_read_file",
        "description": (
            "Read a file's real, current content from the Jarvis repository. Use this "
            "before writing code that touches an area you haven't already read -- "
            "guessing at what exists (a framework, a module, an already-built "
            "integration) produces work that doesn't fit the real codebase."
        ),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path relative to the repo root."},
            "branch_name": {"type": "string", "description": "Defaults to 'main'."},
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "git_create_branch",
        "description": (
            "Create a new git branch off a base branch (default 'main') and check it "
            "out into its own workspace. Always call this before writing any files or "
            "committing — it does nothing to the real deployed code."
        ),
        "parameters": {"type": "object", "properties": {
            "branch_name": {"type": "string", "description": "e.g. 'feature/add-thing'."},
            "base_branch": {"type": "string", "description": "Defaults to 'main'."},
        }, "required": ["branch_name"]},
    }},
    {"type": "function", "function": {
        "name": "git_commit_and_push",
        "description": (
            "Write files (full content, not a diff) into an already-created branch, "
            "then commit and push them. Only affects that branch — nothing is merged or "
            "deployed by this call. Use delete_paths to remove files."
        ),
        "parameters": {"type": "object", "properties": {
            "branch_name": {"type": "string"},
            "files": {
                "type": "array",
                "items": {"type": "object", "properties": {
                    "path": {"type": "string", "description": "Relative path in the repo."},
                    "content": {"type": "string", "description": "Full file content."},
                }, "required": ["path", "content"]},
            },
            "delete_paths": {"type": "array", "items": {"type": "string"}},
            "commit_message": {"type": "string"},
        }, "required": ["branch_name", "commit_message"]},
    }},
    {"type": "function", "function": {
        "name": "git_open_pr",
        "description": "Open a pull request from a pushed branch. CI runs automatically once it's open.",
        "parameters": {"type": "object", "properties": {
            "branch_name": {"type": "string"},
            "title": {"type": "string"},
            "body": {"type": "string", "description": "PR description — what changed and why."},
            "base_branch": {"type": "string", "description": "Defaults to 'main'."},
        }, "required": ["branch_name", "title"]},
    }},
    {"type": "function", "function": {
        "name": "git_get_pr_status",
        "description": "Check a pull request's state and its CI check results.",
        "parameters": {"type": "object", "properties": {
            "pr_number": {"type": "integer"},
        }, "required": ["pr_number"]},
    }},
    {"type": "function", "function": {
        "name": "git_merge_pr",
        "description": (
            "Merge a pull request into main. This is the one real, consequential step "
            "in the git workflow — it actually changes what's on main. Calling this does "
            "not execute it immediately; it stages the action and the owner must "
            "explicitly confirm before it happens."
        ),
        "parameters": {"type": "object", "properties": {
            "pr_number": {"type": "integer"},
            "merge_method": {"type": "string", "enum": ["merge", "squash", "rebase"]},
        }, "required": ["pr_number"]},
    }},
]

GIT_SYSTEM_NOTE = (
    " You also have real git/GitHub tools for development work. git_list_files and "
    "git_read_file show you the actual, current Jarvis codebase — use them before "
    "writing anything. Never assume a tech stack, framework, file layout, or whether an "
    "integration already exists; look first, every time, even if you think you already "
    "know. Guessing wrong produces code that doesn't fit the real repo and is wasted "
    "work. git_create_branch and git_commit_and_push let you stage real code changes on "
    "a branch — always create a branch first, then write files to it, then git_open_pr "
    "to put it up for CI and review. None of that touches main or anything deployed. "
    "git_get_pr_status shows CI results. git_merge_pr is different: it actually changes "
    "what's on main, so calling it does not execute immediately — it stages the action "
    "and you must clearly state which PR and describe what merging it will do, then ask "
    "the owner to explicitly confirm before it happens. Never claim a PR is merged "
    "unless you actually called git_merge_pr and it was confirmed. A PR is not 'done' "
    "just because you opened it, either — check git_get_pr_status yourself before "
    "reporting it as finished; git_merge_pr itself now refuses to merge anything whose "
    "CI isn't fully green and mergeable, so 'opened' and 'ready to merge' are not the "
    "same claim."
)
