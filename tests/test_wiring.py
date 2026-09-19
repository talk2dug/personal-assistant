"""The entrypoints can actually call what they call.

Written after JarvisCore spent six hours in a crash loop, restarting every two minutes,
while 2073 tests passed. main.py threaded a new `cellular_ctx` argument down to
engine.handle_message but not through telegram_bot.build_application in between, so the
process raised TypeError on the line after startup finished, exited 1, and the service
wrapper restarted it forever. Nothing noticed: the crypto feed, the business agents, mail
triage, reminders and location watching all just stopped, and the first sign was a person
saying "the crypto pipeline seems stuck".

Unit tests cannot catch this by construction. Every function involved was individually
correct and individually tested; the defect was in the seam between two of them, and the
seam is only exercised by booting the real process -- which no test does, because booting
it means Telegram, IMAP, a GPU and nine SSH hosts.

So this reads the entrypoints as source and checks every keyword argument they pass to
our own functions against those functions' real signatures. It needs no network, no
config and no process, and it fails on exactly the mistake that caused the outage.
"""
import ast
import importlib
import inspect
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

# The files that wire the application together: a signature mismatch here takes the whole
# process down rather than failing one request. Each names the calls that MUST be reached,
# so the day the resolver stops understanding an import it fails loudly instead of
# quietly checking nothing -- a guard that silently resolves zero calls passes forever.
ENTRYPOINTS = {
    "assistant/main.py": {"build_application", "start"},
    "assistant/web_main.py": {"create_app"},
}


def _imported_modules(tree: ast.Module, package: str) -> dict:
    """Module objects by the name the file refers to them as."""
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            base = "." * node.level + node.module
            for alias in node.names:
                target = f"{base}.{alias.name}" if node.level or True else alias.name
                try:
                    found[alias.asname or alias.name] = importlib.import_module(target, package)
                except ImportError:
                    # A `from x import some_function` -- not a module. Resolved below.
                    try:
                        module = importlib.import_module(base, package)
                    except ImportError:
                        continue
                    attr = getattr(module, alias.name, None)
                    if attr is not None:
                        found[alias.asname or alias.name] = attr
        elif isinstance(node, ast.Import):
            for alias in node.names:
                try:
                    found[alias.asname or alias.name.split(".")[0]] = importlib.import_module(alias.name)
                except ImportError:
                    continue
    return found


def _resolve(node: ast.Call, names: dict):
    """The callable a call node refers to, if we can tell and it is ours."""
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        owner = names.get(func.value.id)
        target = getattr(owner, func.attr, None) if owner is not None else None
    elif isinstance(func, ast.Name):
        target = names.get(func.id)
    else:
        return None
    if not callable(target) or inspect.isclass(target):
        return None
    module = getattr(target, "__module__", "") or ""
    # Only our own code: a signature mismatch against httpx is not ours to police, and
    # third-party decorators make signatures unreliable to read this way.
    return target if module.startswith("assistant") else None


def _calls(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = _imported_modules(tree, package="assistant")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.keywords:
            continue
        # `f(**everything)` tells us nothing statically.
        if any(kw.arg is None for kw in node.keywords):
            continue
        target = _resolve(node, names)
        if target is not None:
            yield node, target


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
def test_every_keyword_the_entrypoint_passes_is_accepted(entrypoint):
    """The exact failure that caused the outage: main.py passed cellular_ctx= to a
    function that had no such parameter, and it only surfaced at runtime."""
    path = REPO / entrypoint
    problems = []
    seen = set()
    for node, target in _calls(path):
        params = inspect.signature(target).parameters
        seen.add(target.__name__)
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
            continue  # **kwargs accepts anything
        for keyword in node.keywords:
            if keyword.arg not in params:
                problems.append(
                    f"{entrypoint}:{node.lineno} passes {keyword.arg}= to "
                    f"{target.__module__}.{target.__qualname__}(), which does not accept it. "
                    f"Accepted: {', '.join(params)}")
    missing = ENTRYPOINTS[entrypoint] - seen
    assert not missing, (
        f"the resolver never reached {sorted(missing)} in {entrypoint} -- the check "
        f"has gone blind; fix it before trusting a pass")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
def test_every_required_argument_is_supplied(entrypoint):
    """The other half of the same seam: a function that gained a required parameter, with
    a caller that was never updated, fails the same way and just as late."""
    path = REPO / entrypoint
    problems = []
    for node, target in _calls(path):
        params = inspect.signature(target).parameters
        given = {kw.arg for kw in node.keywords}
        positional = [p for p in params.values()
                      if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                                    inspect.Parameter.POSITIONAL_OR_KEYWORD)]
        # Positional args in the call cover the first N positional parameters.
        covered = {p.name for p in positional[:len(node.args)]} | given
        if any(isinstance(a, ast.Starred) for a in node.args):
            continue
        missing = [name for name, p in params.items()
                   if p.default is inspect.Parameter.empty
                   and p.kind not in (inspect.Parameter.VAR_POSITIONAL,
                                      inspect.Parameter.VAR_KEYWORD)
                   and name not in covered]
        if missing:
            problems.append(
                f"{entrypoint}:{node.lineno} calls {target.__module__}.{target.__qualname__}() "
                f"without required argument(s): {', '.join(missing)}")
    assert not problems, "\n".join(problems)


def test_the_check_actually_catches_the_bug_it_was_written_for(tmp_path):
    """A guard that silently resolves nothing would pass forever. This proves it fails on
    the real mistake -- calling build_application with the argument that took the service
    down."""
    from assistant.transports import telegram_bot

    params = inspect.signature(telegram_bot.build_application).parameters
    assert "cellular_ctx" in params, (
        "main.py passes cellular_ctx= to build_application; if this parameter is removed "
        "again, JarvisCore will not boot")

    fake = tmp_path / "broken_main.py"
    fake.write_text(
        "from assistant.transports import telegram_bot\n"
        "telegram_bot.build_application(token='t', db_path='d', llm=None, no_such_arg=1)\n",
        encoding="utf-8")
    found = [(node, target) for node, target in _calls(fake)]
    assert found, "the resolver failed to find the call at all"
    node, target = found[0]
    bad = [kw.arg for kw in node.keywords
           if kw.arg not in inspect.signature(target).parameters]
    assert bad == ["no_such_arg"]
