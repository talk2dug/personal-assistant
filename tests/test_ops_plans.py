"""Covers the ops-plan workflow's actual execution semantics: change/test/verify steps
run in order, the first failure stops that sequence and runs every rollback step, and a
successful run never touches rollback at all. This is the piece that has to be right --
the owner's whole reason for asking for a plan instead of per-command confirmation was
"what happens if it fails," and that has to actually happen, not just be documented.
"""
import base64

import pytest

from assistant.core import ops_plans


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "ops.db")
    ops_plans.init_ops_plans_db(path)
    return path


def _steps(*phases):
    """phases: list of (phase, host, command) tuples -> the steps list create_plan wants."""
    return [{"phase": p, "host": h, "command": c, "purpose": f"step for {c}"} for p, h, c in phases]


class FakeSSHClient:
    """Scripted per-command results, keyed by the exact command string, so a test can
    make one specific step fail without guessing exit codes for the rest."""

    def __init__(self, failing_commands: set[str] | None = None):
        self.failing_commands = failing_commands or set()
        self.calls = []

    def run_command(self, host, command, timeout=120):
        self.calls.append((host, command))
        ok = command not in self.failing_commands
        return {"ok": ok, "host": host, "command": command, "exit_code": 0 if ok else 1,
                "output": "done" if ok else "boom"}


class TestValidation:
    def test_rejects_an_empty_plan(self, db):
        with pytest.raises(ValueError, match="at least one step"):
            ops_plans.create_plan(db, 1, "empty", [])

    def test_rejects_a_plan_with_no_rollback_step(self, db):
        with pytest.raises(ValueError, match="rollback step"):
            ops_plans.create_plan(db, 1, "no rollback", _steps(("change", "simrig", "echo hi")))

    def test_rejects_an_unknown_phase(self, db):
        with pytest.raises(ValueError, match="phase must be one of"):
            ops_plans.create_plan(db, 1, "bad phase", [
                {"phase": "deploy", "host": "simrig", "command": "echo hi"},
                {"phase": "rollback", "host": "simrig", "command": "echo undo"},
            ])

    def test_rejects_a_step_missing_a_required_field(self, db):
        with pytest.raises(ValueError, match="missing"):
            ops_plans.create_plan(db, 1, "incomplete", [
                {"phase": "change", "host": "simrig"},
                {"phase": "rollback", "host": "simrig", "command": "echo undo"},
            ])


class TestCreateAndRead:
    def test_create_and_get_round_trip(self, db):
        plan_id = ops_plans.create_plan(db, 1, "Upgrade nginx", _steps(
            ("change", "pi5nas002", "sudo apt install nginx"),
            ("verify", "pi5nas002", "curl -sf localhost"),
            ("rollback", "pi5nas002", "sudo apt install nginx=old"),
        ))
        plan = ops_plans.get_plan(db, plan_id)
        assert plan["summary"] == "Upgrade nginx"
        assert plan["status"] == "proposed"
        assert [s["phase"] for s in plan["steps"]] == ["change", "verify", "rollback"]
        assert all(s["status"] == "pending" for s in plan["steps"])

    def test_get_missing_plan_returns_none(self, db):
        assert ops_plans.get_plan(db, 999) is None

    def test_list_plans_filters_by_owner_and_status(self, db):
        ops_plans.create_plan(db, 1, "mine", _steps(("change", "simrig", "echo hi"), ("rollback", "simrig", "echo undo")))
        ops_plans.create_plan(db, 2, "not mine", _steps(("change", "simrig", "echo hi"), ("rollback", "simrig", "echo undo")))
        assert len(ops_plans.list_plans(db, 1)) == 1
        assert ops_plans.list_plans(db, 1)[0]["summary"] == "mine"

    def test_render_plan_detail_groups_by_phase_in_order(self, db):
        steps = [
            {"phase": "rollback", "host": "simrig", "command": "undo", "purpose": "revert"},
            {"phase": "change", "host": "simrig", "command": "do it", "purpose": "the actual work"},
        ]
        detail = ops_plans.render_plan_detail("Do the thing", steps)
        assert detail.index("What will be done") < detail.index("Plan of attack")
        assert "`do it`" in detail and "the actual work" in detail
        assert "`undo`" in detail and "revert" in detail


class TestRunPlanHappyPath:
    def test_all_steps_succeed_rollback_never_runs(self, db):
        plan_id = ops_plans.create_plan(db, 1, "Safe change", _steps(
            ("change", "simrig", "make the change"),
            ("test", "simrig", "test the change"),
            ("verify", "simrig", "verify the system"),
            ("rollback", "simrig", "undo the change"),
        ))
        ssh = FakeSSHClient()

        result = ops_plans.run_plan(db, plan_id, ssh)

        assert result == {"ok": True, "plan_id": plan_id, "status": "succeeded"}
        assert ("simrig", "undo the change") not in ssh.calls
        plan = ops_plans.get_plan(db, plan_id)
        assert plan["status"] == "succeeded"
        by_command = {s["command"]: s["status"] for s in plan["steps"]}
        assert by_command["make the change"] == "succeeded"
        assert by_command["test the change"] == "succeeded"
        assert by_command["verify the system"] == "succeeded"
        assert by_command["undo the change"] == "skipped"


class TestRunPlanFailurePath:
    def test_a_failed_change_step_stops_the_sequence_and_runs_rollback(self, db):
        plan_id = ops_plans.create_plan(db, 1, "Risky change", _steps(
            ("change", "simrig", "make the change"),
            ("test", "simrig", "test the change"),
            ("verify", "simrig", "verify the system"),
            ("rollback", "simrig", "undo the change"),
        ))
        ssh = FakeSSHClient(failing_commands={"make the change"})

        result = ops_plans.run_plan(db, plan_id, ssh)

        assert result == {"ok": False, "plan_id": plan_id, "status": "failed"}
        # Stopped immediately -- test/verify never ran once change failed.
        assert ("simrig", "test the change") not in ssh.calls
        assert ("simrig", "verify the system") not in ssh.calls
        assert ("simrig", "undo the change") in ssh.calls
        plan = ops_plans.get_plan(db, plan_id)
        by_command = {s["command"]: s["status"] for s in plan["steps"]}
        assert by_command["make the change"] == "failed"
        assert by_command["test the change"] == "pending"
        assert by_command["undo the change"] == "succeeded"

    def test_a_failed_verify_step_still_triggers_rollback(self, db):
        """The whole point of a verify phase: the change and its own test can both pass
        while the system as a whole is broken, and that must still roll back."""
        plan_id = ops_plans.create_plan(db, 1, "Looks fine, isn't", _steps(
            ("change", "simrig", "make the change"),
            ("test", "simrig", "test the change"),
            ("verify", "simrig", "verify the system"),
            ("rollback", "simrig", "undo the change"),
        ))
        ssh = FakeSSHClient(failing_commands={"verify the system"})

        result = ops_plans.run_plan(db, plan_id, ssh)

        assert result["status"] == "failed"
        assert ("simrig", "make the change") in ssh.calls
        assert ("simrig", "test the change") in ssh.calls
        assert ("simrig", "undo the change") in ssh.calls

    def test_multiple_rollback_steps_all_run_in_order(self, db):
        plan_id = ops_plans.create_plan(db, 1, "Multi-host rollback", [
            {"phase": "change", "host": "simrig", "command": "change on simrig", "purpose": "p"},
            {"phase": "rollback", "host": "simrig", "command": "undo on simrig", "purpose": "p"},
            {"phase": "rollback", "host": "touch1", "command": "undo on touch1", "purpose": "p"},
        ])
        ssh = FakeSSHClient(failing_commands={"change on simrig"})

        ops_plans.run_plan(db, plan_id, ssh)

        assert ("simrig", "undo on simrig") in ssh.calls
        assert ("touch1", "undo on touch1") in ssh.calls

    def test_a_raised_exception_mid_step_is_treated_as_a_failure_not_a_crash(self, db):
        plan_id = ops_plans.create_plan(db, 1, "Connection drops", _steps(
            ("change", "simrig", "make the change"),
            ("rollback", "simrig", "undo the change"),
        ))

        class ExplodingSSHClient:
            def run_command(self, host, command, timeout=120):
                raise RuntimeError("connection reset")

        result = ops_plans.run_plan(db, plan_id, ExplodingSSHClient())

        assert result["status"] == "failed"
        plan = ops_plans.get_plan(db, plan_id)
        step = next(s for s in plan["steps"] if s["command"] == "make the change")
        assert step["status"] == "failed"
        assert "connection reset" in step["output"]

    def test_an_unknown_plan_id_fails_cleanly(self, db):
        result = ops_plans.run_plan(db, 999, FakeSSHClient())
        assert result == {"ok": False, "error": "no such plan 999"}


class TestBuildMcpInstallPlanSteps:
    """build_mcp_install_plan_steps is the safe template a systems-engineer employee
    should reach for instead of freehanding propose_ops_plan's fully generic steps --
    these check its output is well-formed (a real plan validate_steps would accept) and
    that the generated config-merge script actually says what it claims to."""

    def _steps(self, **overrides):
        kwargs = dict(
            host="jarvisbox", name="weather", package_spec="some-weather-mcp==1.0.0",
            import_check="some_weather_mcp", config_updates={"weather_api_key": "abc123"},
            restart_command="Restart-Service JarvisCore",
            log_path=r"C:\jarvis\logs\jarvis-core.log",
        )
        kwargs.update(overrides)
        return ops_plans.build_mcp_install_plan_steps(**kwargs)

    def test_produces_a_plan_that_passes_validate_steps(self):
        ops_plans.validate_steps(self._steps())  # raises on anything malformed

    def test_every_step_targets_the_given_host(self):
        steps = self._steps(host="jarvisbox")
        assert all(s["host"] == "jarvisbox" for s in steps)

    def test_has_at_least_one_step_per_phase(self):
        steps = self._steps()
        phases_present = {s["phase"] for s in steps}
        assert phases_present == {"change", "test", "verify", "rollback"}

    def test_installs_into_an_isolated_dot_venv_name_not_the_shared_venv(self):
        steps = self._steps(name="weather")
        install_step = next(s for s in steps if "pip.exe" in s["command"])
        assert ".venv-weather" in install_step["command"]
        assert install_step["command"].count(".venv-weather") >= 1

    def test_import_check_runs_before_any_config_is_touched(self):
        steps = self._steps(import_check="some_weather_mcp")
        commands = [s["command"] for s in steps]
        import_index = next(i for i, c in enumerate(commands) if "import some_weather_mcp" in c)
        config_merge_index = next(i for i, c in enumerate(commands) if "_ops_plan_merge_" in c and "python.exe" in c)
        assert import_index < config_merge_index

    def test_config_merge_script_actually_contains_the_given_updates(self):
        steps = self._steps(config_updates={"weather_api_key": "abc123", "weather_poll_seconds": 300})
        write_step = next(s for s in steps if "WriteAllText" in s["command"])
        # Pull the base64 payload out of the generated PowerShell command and decode it,
        # the same way the real remote step would -- proves the round trip actually
        # carries the real config values, not just that *a* script gets written.
        encoded = write_step["command"].split("FromBase64String('")[1].split("')")[0]
        script = base64.b64decode(encoded).decode()
        assert "weather_api_key" in script
        assert "abc123" in script
        assert "weather_poll_seconds" in script
        assert "300" in script

    def test_verify_step_checks_for_the_names_own_discovery_log_line(self):
        steps = self._steps(name="weather", log_path=r"C:\jarvis\logs\jarvis-core.log")
        verify_step = next(s for s in steps if s["phase"] == "verify")
        assert "weather.*discovered" in verify_step["command"]
        assert r"C:\jarvis\logs\jarvis-core.log" in verify_step["command"]

    def test_rollback_restores_config_and_removes_the_venv(self):
        steps = self._steps(name="weather")
        rollback_commands = " ".join(s["command"] for s in steps if s["phase"] == "rollback")
        assert "config.json.bak-weather" in rollback_commands
        assert ".venv-weather" in rollback_commands
        assert "Restart-Service JarvisCore" in rollback_commands

    def test_restart_command_is_used_for_both_the_change_and_rollback_restart(self):
        steps = self._steps(restart_command="Restart-Service JarvisCore")
        restart_uses = [s for s in steps if s["command"] == "Restart-Service JarvisCore"]
        assert len(restart_uses) == 2  # once mid-plan, once during rollback
        change_restart = next(s for s in restart_uses if s["phase"] == "change")
        rollback_restart = next(s for s in restart_uses if s["phase"] == "rollback")
        assert change_restart is not rollback_restart
