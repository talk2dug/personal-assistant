"""setup._root_cause: unwraps an asyncio.TaskGroup's ExceptionGroup (and a normal
raised-from chain) to find the actual underlying error, instead of logging the useless
'unhandled errors in a TaskGroup (1 sub-exception)' wrapper every async MCP client
startup failure (Era/phone/Kroger/recipe) otherwise produces regardless of what broke.
"""
from assistant.core.setup import _root_cause


def test_plain_exception_reports_itself():
    exc = ValueError("bad config")
    assert _root_cause(exc) == "ValueError: bad config"


def test_unwraps_one_level_of_exception_group():
    inner = FileNotFoundError("kroger-mcp.exe missing")
    group = ExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)", [inner])
    assert _root_cause(group) == "FileNotFoundError: kroger-mcp.exe missing"


def test_unwraps_nested_exception_groups():
    innermost = TimeoutError("MCP handshake never completed")
    middle = ExceptionGroup("outer", [ExceptionGroup("inner", [innermost])])
    assert _root_cause(middle) == "TimeoutError: MCP handshake never completed"


def test_follows_a_raised_from_cause_chain():
    try:
        try:
            raise ConnectionRefusedError("port 8000 refused")
        except ConnectionRefusedError as e:
            raise RuntimeError("could not start server") from e
    except RuntimeError as outer:
        assert _root_cause(outer) == "ConnectionRefusedError: port 8000 refused"


def test_follows_implicit_context_when_no_explicit_cause():
    try:
        try:
            raise KeyError("KROGER_CLIENT_ID")
        except KeyError:
            raise RuntimeError("startup failed")  # no `from` -- implicit __context__
    except RuntimeError as outer:
        assert _root_cause(outer) == "KeyError: 'KROGER_CLIENT_ID'"


def test_never_loops_forever_on_a_self_referential_chain():
    """Defensive: __context__ can technically end up pointing back at an exception
    already seen if something reuses/reraises the same object -- must terminate, not
    hang starting every service."""
    exc = RuntimeError("weird")
    exc.__context__ = exc
    assert _root_cause(exc) == "RuntimeError: weird"
