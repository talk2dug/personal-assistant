"""Chat-facing tool schemas for git_ops.py — same split as kroger_recipe.py: the schema
list and system note live here, the actual client lives in git_ops.py.
"""

GIT_TOOLS = [
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
    " You also have real git/GitHub tools for development work. git_create_branch and "
    "git_commit_and_push let you stage real code changes on a branch — always create a "
    "branch first, then write files to it, then git_open_pr to put it up for CI and "
    "review. None of that touches main or anything deployed. git_get_pr_status shows CI "
    "results. git_merge_pr is different: it actually changes what's on main, so calling "
    "it does not execute immediately — it stages the action and you must clearly state "
    "which PR and describe what merging it will do, then ask the owner to explicitly "
    "confirm before it happens. Never claim a PR is merged unless you actually called "
    "git_merge_pr and it was confirmed."
)
