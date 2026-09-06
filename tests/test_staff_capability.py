"""Covers the "execute" capability tier: the one tier that can act (real git tools)
rather than only report. Mirrors the project's existing lesson about infer_data_feeds's
"paper" feed (see staff.py's comment above CAPABILITY_TIERS) -- a real capability must
never be inferred from job-description wording, only granted by an explicit, separate
action. These tests pin that a title like "Systems Engineer" or "DevOps Lead" alone
never grants it, that granting/revoking it works and survives a job-description rewrite,
and that assign() actually routes execute-tier work through llm.engineer() instead of
llm.research().
"""
import pytest

from assistant.core import staff


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "staff.db")
    staff.init_staff_db(path)
    return path


class TestExecuteTierIsNeverInferred:
    @pytest.mark.parametrize("title,description", [
        ("Systems Engineer", "10 years of experience running Linux infrastructure and CI/CD pipelines."),
        ("DevOps Lead", "Owns our deployment pipeline and server fleet."),
        ("SRE", "Keeps production systems reliable, on-call rotation experience."),
        ("Senior Developer", "Reports to the devops team and works closely with our SRE on deploys."),
    ])
    def test_hiring_never_lands_in_execute_tier_from_wording_alone(self, db, title, description):
        emp = staff.hire(db, title, description)
        assert emp["capability_tier"] != "execute"
        # These all land in "authoring" (engineering department) -- same as any other
        # developer -- confirming the keyword match isn't accidentally hitting a new
        # execute-mapped department either.
        assert emp["capability_tier"] == "authoring"

    def test_capability_tiers_map_contains_no_execute_department(self):
        """execute must only ever be reachable via set_capability_override -- if a
        department ever mapped to it directly, hiring could grant it by wording alone."""
        assert "execute" not in staff.CAPABILITY_TIERS.values()


class TestSetCapabilityOverride:
    def test_grants_execute_tier_and_recompiles_the_prompt(self, db):
        staff.hire(db, "Systems Engineer", "Runs our infrastructure and deploy pipeline.")
        emp = staff.set_capability_override(db, "systems_engineer", "execute")
        assert emp["capability_tier"] == "execute"
        assert emp["capability_override"] == "execute"
        assert "carry out real changes" in emp["system_prompt"]

    def test_revoking_reverts_to_the_inferred_tier(self, db):
        staff.hire(db, "Systems Engineer", "Runs our infrastructure and deploy pipeline.")
        staff.set_capability_override(db, "systems_engineer", "execute")
        emp = staff.set_capability_override(db, "systems_engineer", None)
        assert emp["capability_tier"] == "authoring"
        assert emp["capability_override"] is None

    def test_rejects_an_unknown_tier(self, db):
        staff.hire(db, "Systems Engineer", "Runs our infrastructure and deploy pipeline.")
        with pytest.raises(ValueError, match="unknown capability tier"):
            staff.set_capability_override(db, "systems_engineer", "root_access")

    def test_returns_none_for_a_missing_employee(self, db):
        assert staff.set_capability_override(db, "nobody", "execute") is None

    def test_survives_a_job_description_rewrite(self, db):
        """The exact regression this feature has to avoid: revise_job_description always
        recomputes tier from department inference, which never produces 'execute' -- an
        explicit grant must not be silently wiped out by an unrelated edit."""
        staff.hire(db, "Systems Engineer", "Runs our infrastructure and deploy pipeline.")
        staff.set_capability_override(db, "systems_engineer", "execute")

        emp = staff.revise_job_description(
            db, "systems_engineer", "Now also owns the Kubernetes migration, 5 years experience.")

        assert emp["capability_tier"] == "execute"
        assert emp["capability_override"] == "execute"

    def test_a_normal_employee_with_no_override_is_unaffected_by_revision(self, db):
        staff.hire(db, "Research Analyst", "Tracks industry trends and publishes findings.")
        emp = staff.revise_job_description(db, "research_analyst", "Also builds internal dashboards now.")
        assert emp["capability_tier"] == "research"
        assert emp["capability_override"] is None


class FakeLLMWithEngineer:
    def __init__(self, engineer_output="branch pushed, PR opened"):
        self.research_calls = []
        self.engineer_calls = []
        self._engineer_output = engineer_output

    def research(self, prompt, system_prompt=None, timeout=None):
        self.research_calls.append((prompt, system_prompt, timeout))
        return "researched it"

    def engineer(self, prompt, system_prompt=None, tools=None, timeout=None):
        self.engineer_calls.append((prompt, system_prompt, tools, timeout))
        return self._engineer_output


class FakeLLMWithoutEngineer:
    """The Ollama backend has no engineer() (or research()) at all -- mirrors that
    real gap so assign() must fail cleanly rather than raising AttributeError."""

    def research(self, prompt, system_prompt=None, timeout=None):
        return "researched it"


class TestAssignRoutesByCapabilityTier:
    def test_execute_tier_employee_runs_through_engineer_not_research(self, db):
        staff.hire(db, "Systems Engineer", "Runs our infrastructure.")
        staff.set_capability_override(db, "systems_engineer", "execute")
        llm = FakeLLMWithEngineer()

        result = staff.assign(db, llm, "systems_engineer", "Push the config fix to a branch")

        assert result["ok"] is True
        assert result["output"] == "branch pushed, PR opened"
        assert len(llm.engineer_calls) == 1
        assert llm.research_calls == []
        # The real, narrowly-scoped git tool set was passed, not an empty/owner catalog.
        _, _, tools, _ = llm.engineer_calls[0]
        assert any(t["function"]["name"] == "git_create_branch" for t in tools)

    def test_normal_employee_still_runs_through_research(self, db):
        staff.hire(db, "Copywriter", "Writes marketing copy.")
        llm = FakeLLMWithEngineer()

        result = staff.assign(db, llm, "copywriter", "Write a product description")

        assert result["ok"] is True
        assert len(llm.research_calls) == 1
        assert llm.engineer_calls == []

    def test_execute_tier_on_a_backend_without_engineer_fails_cleanly(self, db):
        staff.hire(db, "Systems Engineer", "Runs our infrastructure.")
        staff.set_capability_override(db, "systems_engineer", "execute")
        llm = FakeLLMWithoutEngineer()

        result = staff.assign(db, llm, "systems_engineer", "Push the config fix")

        assert result["ok"] is False
        assert "engineer" in result["error"].lower()
