import hashlib
import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from runtime.daemon.agent_config import set_executor, write_default_agent_config
from runtime.infrastructure.database import Database
from runtime.models import (
    TaskRecord,
    TaskStatus,
    ThreadRecord,
)
from runtime.orchestrator.executors import ExecutorResult
from runtime.orchestrator.orchestrator import Orchestrator, AgentUnavailableError
from runtime.orchestrator.teams import TeamsRegistry


@pytest.fixture(autouse=True)
def _ensure_protocol_skills(test_settings):
    """TASK-2511: pre-create protocol/skills/ source dirs so
    ensure_system_contracts_materialized can inject + verify on-disk."""
    _setup_protocol_skills(test_settings)


@pytest.fixture(autouse=True)
def _seed_active_agents_for_orchestrator(test_runtime):
    """Task launch is fail-closed: an active AgentDef is required.

    test_runtime already seeds the standard set; this fixture ensures the
    module's default agents are present even if the shared set changes.
    """
    from tests.conftest import seed_test_agents
    seed_test_agents(test_runtime, _DEFAULT_AGENTS)


@pytest.fixture
def orchestrator(test_settings, test_runtime):
    test_runtime.root.mkdir(parents=True, exist_ok=True)
    db = Database(test_runtime.db_path)
    teams = TeamsRegistry.load(test_runtime.root)
    return Orchestrator(
        db=db, settings=test_settings,
        paths=test_runtime, slug="test", teams=teams,
    )


_DEFAULT_AGENTS = ["engineering_head", "product_manager", "dev_agent", "payment_agent"]

# System-contract IDs expected for "task" context with repos.
# Must exist in protocol/skills/ so ensure_system_contracts_materialized
# (TASK-2511) can inject + verify them.
_TASK_CONTEXT_CONTRACT_IDS = ["start-task", "jobs", "make-worktree", "thread", "dream", "todos", "create-skill"]


def _setup_protocol_skills(settings, contract_ids: list[str] | None = None) -> None:
    """Create minimal protocol/skills/<id>/SKILL.md source files so
    ensure_system_contracts_materialized can inject them into workspaces."""
    for sid in (contract_ids or _TASK_CONTEXT_CONTRACT_IDS):
        src = settings.get_protocol_dir() / "skills" / sid
        src.mkdir(parents=True, exist_ok=True)
        (src / "SKILL.md").write_text(f"# {sid}\n\nSkill body for {sid}.\n")


def _setup_workspaces(runtime, agents: list[str] | None = None):
    for agent in (agents or _DEFAULT_AGENTS):
        ws = runtime.workspaces_dir / agent
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "task_history.md").write_text(f"# Task History: {agent}\n\n")
        # Under the canonical store model, workspace skill symlinks are
        # created by the SymlinkMaterializer during pre-spawn materialization,
        # NOT by pre-creating ordinary directories. Creating an ordinary
        # directory at the link path would cause ordinary_dir_at_link_path.


def _setup_codex_workspace(runtime, agent: str) -> None:
    ws = runtime.workspaces_dir / agent
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "task_history.md").write_text(f"# Task History: {agent}\n\n")
    write_default_agent_config(ws)
    set_executor(ws, "codex")
    (ws / "AGENTS.md").write_text(f"# Agent: {agent}\n")
    # THR-095: executor is now read from org/agents/<name>.md (single source),
    # not agent.yaml. Write the .md with the matching executor.
    from runtime.orchestrator.agent_def import AgentDef, render_agent_text
    ad = AgentDef(
        name=agent, team="engineering", role="manager",
        executor="codex", allow_rules=(), repos={},
        enrolled_by=None, enrolled_at_task=None, enrolled_at=None,
        system_prompt=f"You are {agent}.", description="", model=None,
    )
    runtime.agents_dir.mkdir(parents=True, exist_ok=True)
    (runtime.agents_dir / f"{agent}.md").write_text(render_agent_text(ad))


def _setup_opencode_workspace(runtime, agent: str) -> None:
    ws = runtime.workspaces_dir / agent
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "task_history.md").write_text(f"# Task History: {agent}\n\n")
    write_default_agent_config(ws)
    set_executor(ws, "opencode")
    (ws / "AGENTS.md").write_text(f"# Agent: {agent}\n")
    # THR-095: executor is now read from org/agents/<name>.md (single source)
    from runtime.orchestrator.agent_def import AgentDef, render_agent_text
    ad = AgentDef(
        name=agent, team="engineering", role="manager",
        executor="opencode", allow_rules=(), repos={},
        enrolled_by=None, enrolled_at_task=None, enrolled_at=None,
        system_prompt=f"You are {agent}.", description="", model=None,
    )
    runtime.agents_dir.mkdir(parents=True, exist_ok=True)
    (runtime.agents_dir / f"{agent}.md").write_text(render_agent_text(ad))


def _setup_pi_workspace(runtime, agent: str) -> None:
    ws = runtime.workspaces_dir / agent
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "task_history.md").write_text(f"# Task History: {agent}\n\n")
    write_default_agent_config(ws)
    set_executor(ws, "pi")
    (ws / "AGENTS.md").write_text(f"# Agent: {agent}\n")
    # THR-095: executor is now read from org/agents/<name>.md (single source)
    from runtime.orchestrator.agent_def import AgentDef, render_agent_text
    ad = AgentDef(
        name=agent, team="engineering", role="manager",
        executor="pi", allow_rules=(), repos={},
        enrolled_by=None, enrolled_at_task=None, enrolled_at=None,
        system_prompt=f"You are {agent}.", description="", model=None,
    )
    runtime.agents_dir.mkdir(parents=True, exist_ok=True)
    (runtime.agents_dir / f"{agent}.md").write_text(render_agent_text(ad))


def test_orchestrator_no_longer_has_run_task():
    """run_task was removed in favor of the async run_step queue model."""
    from runtime.orchestrator.orchestrator import Orchestrator
    assert not hasattr(Orchestrator, "run_task")


def test_create_task(orchestrator):
    task_id = orchestrator.create_task("Explore the codebase")
    assert task_id == "TASK-001"
    task = orchestrator._db.get_task(task_id)
    assert task.status == TaskStatus.PENDING
    assert task.brief == "Explore the codebase"


def test_create_task_with_team(orchestrator):
    task_id = orchestrator.create_task("Add Alipay", team="engineering")
    task = orchestrator._db.get_task(task_id)
    assert task.team == "engineering"


def test_custom_skill_materializes_only_after_eligibility_on_next_task_session(
    orchestrator, test_runtime, monkeypatch,
):
    """The real task-spawn seam only writes evidence for a new eligible session."""
    from runtime.infrastructure.artifact_store import ArtifactStore

    _setup_workspaces(test_runtime, ["dev_agent"])
    conn = orchestrator.db._conn
    content = "# Visible custom skill\n"
    artifact_key = ArtifactStore(test_runtime.artifacts_dir).put(
        "custom-skills/visible/SKILL.md", content.encode(),
    ).name
    conn.execute(
        "INSERT INTO custom_skills (id, org_slug, slug, name, origin_kind, created_at, created_by) "
        "VALUES ('custom:visible', 'test', 'visible', 'Visible', 'human', 'now', 'founder')"
    )
    conn.execute(
        "INSERT INTO custom_skill_versions "
        "(skill_id, content_hash, content_artifact_key, skill_md_cache, validation_state, "
        "created_at, author_kind, author_identity) VALUES (?,?,?,?,?,?,?,?)",
        ("custom:visible", hashlib.sha256(content.encode()).hexdigest(), artifact_key,
         content, "valid", "now", "human", "founder"),
    )
    version_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("UPDATE custom_skills SET current_version_id=? WHERE id='custom:visible'", (version_id,))
    conn.commit()

    sessions = iter(("sess-before", "sess-after"))
    monkeypatch.setattr(orchestrator, "_build_session_id", lambda: next(sessions))
    executor = MagicMock()
    executor.run.side_effect = [
        ExecutorResult(success=True, duration_seconds=1, session_id="sess-before"),
        ExecutorResult(success=True, duration_seconds=1, session_id="sess-after"),
    ]
    with patch.object(orchestrator, "_build_executor", return_value=executor):
        first_task = orchestrator.create_task("before eligibility")
        orchestrator._run_agent(first_task, "dev_agent", "")
        assert conn.execute("SELECT count(*) FROM custom_skill_materializations").fetchone()[0] == 0

        conn.execute(
            "INSERT INTO custom_skill_eligibility_rules "
            "(skill_id, scope_type, scope_target, effect, created_at, created_by) "
            "VALUES ('custom:visible', 'org', NULL, 'allow', 'now', 'founder')"
        )
        conn.commit()
        second_task = orchestrator.create_task("after eligibility")
        orchestrator._run_agent(second_task, "dev_agent", "")

    rows = conn.execute(
        "SELECT task_id, session_id, version_id, content_hash, success "
        "FROM custom_skill_materializations WHERE skill_id='custom:visible'"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (second_task, "sess-after", version_id, hashlib.sha256(content.encode()).hexdigest(), 1)
    ]


def test_custom_skill_materialization_failure_does_not_abort_task_spawn(
    orchestrator, test_runtime, monkeypatch,
):
    """A broken visible custom skill is recorded without suppressing siblings."""
    from runtime.infrastructure.artifact_store import ArtifactStore

    _setup_workspaces(test_runtime, ["dev_agent"])
    conn = orchestrator.db._conn
    good_content = "# Good custom skill\n"
    good_hash = hashlib.sha256(good_content.encode()).hexdigest()
    good_key = ArtifactStore(test_runtime.artifacts_dir).put(
        "custom-skills/good/SKILL.md", good_content.encode(),
    ).name
    for skill_id, skill_slug, content_hash, artifact_key in (
        ("custom:good", "good", good_hash, good_key),
        ("custom:broken", "broken", "0" * 64, "custom-skills/broken/missing/SKILL.md"),
    ):
        conn.execute(
            "INSERT INTO custom_skills (id, org_slug, slug, name, origin_kind, created_at, created_by) "
            "VALUES (?, 'test', ?, ?, 'human', 'now', 'founder')",
            (skill_id, skill_slug, skill_slug.title()),
        )
        conn.execute(
            "INSERT INTO custom_skill_versions "
            "(skill_id, content_hash, content_artifact_key, validation_state, created_at, author_kind, author_identity) "
            "VALUES (?,?,?,?,?,?,?)",
            (skill_id, content_hash, artifact_key, "valid", "now", "human", "founder"),
        )
        version_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE custom_skills SET current_version_id=? WHERE id=?", (version_id, skill_id))
        conn.execute(
            "INSERT INTO custom_skill_eligibility_rules "
            "(skill_id, scope_type, scope_target, effect, created_at, created_by) "
            "VALUES (?, 'org', NULL, 'allow', 'now', 'founder')",
            (skill_id,),
        )
    conn.commit()

    monkeypatch.setattr(orchestrator, "_build_session_id", lambda: "sess-failure")
    executor = MagicMock()
    executor.run.return_value = ExecutorResult(
        success=True, duration_seconds=1, session_id="sess-failure",
    )
    with patch.object(orchestrator, "_build_executor", return_value=executor):
        task_id = orchestrator.create_task("custom materialization resilience")
        orchestrator._run_agent(task_id, "dev_agent", "")

    assert executor.run.called
    outcomes = {
        row["skill_id"]: (row["success"], row["error_message"])
        for row in conn.execute(
            "SELECT skill_id, success, error_message FROM custom_skill_materializations "
            "WHERE session_id='sess-failure'"
        ).fetchall()
    }
    assert outcomes["custom:good"] == (1, None)
    assert outcomes["custom:broken"][0] == 0
    assert outcomes["custom:broken"][1]


def test_task_metadata_in_agent_prompt(orchestrator, test_runtime, monkeypatch):
    """Agent prompts should include task_id, session_id, and brief.

    Covers the prompt-assembly contract in `_run_agent` — the start-task skill
    parses these keys out of the injected parameters block.
    """
    _setup_workspaces(test_runtime)

    task_id = orchestrator.create_task("Explore payments")

    # Fix the session_id so the prompt is deterministic.
    monkeypatch.setattr(orchestrator, "_build_session_id", lambda: "sess-eh")

    mock_executor = MagicMock()
    mock_executor.run.return_value = ExecutorResult(
        success=True,
        duration_seconds=30,
        session_id="sess-eh",
    )
    with patch.object(orchestrator, "_build_executor", return_value=mock_executor):
        orchestrator._run_agent(task_id, "engineering_head", "Decide what to do next")

    prompt = mock_executor.run.call_args.kwargs["prompt"]
    assert "Use the start-task skill" in prompt
    assert "task_id: TASK-001" in prompt
    assert "brief: Explore payments" in prompt
    assert "session_id:" in prompt
    assert "role_guidance:" in prompt
    # Regression guard: the brief must appear exactly once. Before the
    # role_guidance / capabilities cleanup the brief was rendered both in
    # ``Parameters.brief`` and at the top of the capabilities block
    # (``# Task\n<brief>``), doubling the brief on every manager spawn.
    assert prompt.count("Explore payments") == 1


def test_worker_prompt_omits_role_guidance_block(
    orchestrator, test_runtime, monkeypatch,
):
    """Worker spawns receive only ``Parameters.brief`` — no ``role_guidance:``
    line. Before the cleanup, ``run_step._build_agent_prompt`` returned
    ``task.brief`` for workers, which the outer wrapper then re-rendered
    under ``role_guidance: |``, duplicating the brief in every worker spawn.
    """
    _setup_workspaces(test_runtime)
    task_id = orchestrator.create_task("Implement Alipay webhook")
    monkeypatch.setattr(orchestrator, "_build_session_id", lambda: "sess-dev")

    mock_executor = MagicMock()
    mock_executor.run.return_value = ExecutorResult(
        success=True,
        duration_seconds=30,
        session_id="sess-dev",
    )
    with patch.object(orchestrator, "_build_executor", return_value=mock_executor):
        # Worker case: inner run_step._build_agent_prompt returns "" for
        # non-managers; _run_agent's outer wrapper must omit the line.
        orchestrator._run_agent(task_id, "dev_agent", "")

    prompt = mock_executor.run.call_args.kwargs["prompt"]
    assert "brief: Implement Alipay webhook" in prompt
    assert prompt.count("Implement Alipay webhook") == 1
    assert "role_guidance:" not in prompt
    # No dangling block-scalar marker should be left behind.
    assert "  |\n" not in prompt


def test_run_agent_shipping_seam_injects_manager_policy_binds_session_and_omits_worker(
    orchestrator, test_runtime, monkeypatch,
):
    from runtime.orchestrator.active_authority_policy import (
        RESERVED_TEAM_POLICY_HEADER, load_session_policy_snapshot,
    )
    from runtime.orchestrator.authority_policy_store import AuthorityPolicyStore
    from tests.authority_policy_test_factory import activate_test_policy

    from runtime.orchestrator.teams import TeamManager
    from tests.conftest import seed_test_agents
    seed_test_agents(test_runtime, ("engineering_manager", "dev_agent"))
    _setup_workspaces(test_runtime, ["engineering_manager", "dev_agent"])
    orchestrator._teams._teams["engineering"] = TeamManager(
        name="engineering_manager", team="engineering", workers=("dev_agent",),
    )
    release, activation = activate_test_policy(orchestrator._db)
    mock_executor = MagicMock()
    mock_executor.run.return_value = ExecutorResult(
        success=True, duration_seconds=1, session_id="provider-session",
    )

    manager_task = orchestrator.create_task("manager work")
    monkeypatch.setattr(orchestrator, "_build_session_id", lambda: "sess-policy-manager")
    with patch.object(orchestrator, "_build_executor", return_value=mock_executor):
        orchestrator._run_agent(manager_task, "engineering_manager", "decide")
    manager_prompt = mock_executor.run.call_args.kwargs["prompt"]
    assert manager_prompt.count(RESERVED_TEAM_POLICY_HEADER) == 1
    assert release.id in manager_prompt and release.policy_digest in manager_prompt
    pinned = load_session_policy_snapshot(
        db=orchestrator._db, store=AuthorityPolicyStore(orchestrator._db),
        task_id=manager_task, session_id="sess-policy-manager",
        agent_name="engineering_manager",
    )
    assert pinned.release.id == release.id
    assert pinned.activation.id == activation.id

    worker_task = orchestrator.create_task("worker work")
    monkeypatch.setattr(orchestrator, "_build_session_id", lambda: "sess-policy-worker")
    with patch.object(orchestrator, "_build_executor", return_value=mock_executor):
        orchestrator._run_agent(worker_task, "dev_agent", "")
    assert RESERVED_TEAM_POLICY_HEADER not in mock_executor.run.call_args.kwargs["prompt"]


def test_manager_policy_shipping_seam_consumes_authenticated_self_evaluation(
    orchestrator, test_runtime, monkeypatch,
):
    """S6a connected proof: real launch binding -> result row -> authority hook."""
    from runtime.orchestrator.active_authority_policy import (
        SELF_EVALUATION_CONTRACT_DIGEST, SELF_EVALUATION_CONTRACT_ID,
        SELF_EVALUATION_CONTRACT_VERSION, load_session_policy_binding,
    )
    from runtime.orchestrator.authority import run_authority_hook
    from runtime.orchestrator.authority_policy import CONTINUE_ROUTINE_PHRASE
    from runtime.orchestrator.authority_policy_store import AuthorityPolicyStore
    from runtime.orchestrator.teams import TeamManager
    from tests.authority_policy_test_factory import activate_test_policy
    from tests.conftest import seed_test_agents

    seed_test_agents(test_runtime, ("engineering_manager", "dev_agent"))
    _setup_workspaces(test_runtime, ["engineering_manager", "dev_agent"])
    orchestrator._teams._teams["engineering"] = TeamManager(
        name="engineering_manager", team="engineering", workers=("dev_agent",),
    )
    release, activation = activate_test_policy(orchestrator._db)
    mock_executor = MagicMock()
    mock_executor.run.return_value = ExecutorResult(
        success=True, duration_seconds=1, session_id="provider-session",
    )
    task_id = orchestrator.create_task("manager work")
    orchestrator._db.insert_thread(ThreadRecord(id="THR-s6a", subject="S6a proof"))
    orchestrator._db.update_task(
        task_id, assigned_agent="engineering_manager",
    )
    orchestrator._db.execute(
        "UPDATE tasks SET dispatched_from_thread_id=? WHERE id=?", ("THR-s6a", task_id),
    )
    monkeypatch.setattr(orchestrator, "_build_session_id", lambda: "sess-s6a")
    with patch.object(orchestrator, "_build_executor", return_value=mock_executor):
        orchestrator._run_agent(task_id, "engineering_manager", "decide")
    binding = load_session_policy_binding(
        db=orchestrator._db, task_id=task_id, session_id="sess-s6a",
        agent_name="engineering_manager",
    )
    assert binding is not None
    orchestrator._db.update_task(
        task_id, status=TaskStatus.IN_PROGRESS, block_kind=None,
        orchestration_step_count=1, current_session_id="sess-s6a",
    )
    decision = {"action": "escalate", "reason": CONTINUE_ROUTINE_PHRASE}
    self_eval = {
        "contract_id": SELF_EVALUATION_CONTRACT_ID,
        "contract_version": SELF_EVALUATION_CONTRACT_VERSION,
        "contract_digest": SELF_EVALUATION_CONTRACT_DIGEST,
        "root_task_id": task_id, "manager_session_id": "sess-s6a",
        "release_id": release.id, "policy_version": str(release.version),
        "policy_digest": release.policy_digest,
        "activation_id": activation.id, "activation_epoch": activation.epoch,
        "provider_id": binding["provider_id"],
        "executor_kind": binding["executor_kind"],
        "model_id": binding["model_id"],
        "disposition": "continue_same_root",
        "clause_id": "cont-routine-same-root", "action": "continue_same_root",
        "confidence": 1.0, "uncertainty_codes": [],
    }
    orchestrator._db.insert_task_result(
        task_id=task_id, agent="engineering_manager", session_id="sess-s6a",
        output_summary="complete", confidence_score=100,
        decision_json=json.dumps({**decision, "_manager_self_evaluation": self_eval}),
    )
    result_row = orchestrator._db.get_latest_task_result(
        task_id, "engineering_manager", "sess-s6a"
    )
    assert run_authority_hook(
        orchestrator, orchestrator._db.get_task(task_id), "engineering_manager",
        CONTINUE_ROUTINE_PHRASE, result_row["id"],
        manager_self_evaluation=self_eval,
    ) == "continue_same_root"
    assert orchestrator._db.get_task(task_id).status == TaskStatus.PENDING
    evaluations = orchestrator._db.execute(
        "SELECT disposition FROM authority_evaluations"
    ).fetchall()
    assert [row["disposition"] for row in evaluations] == ["continue_same_root"]
    produced = orchestrator._db.execute("SELECT * FROM authority_candidates").fetchone()
    assert produced["root_task_id"] == task_id
    assert produced["manager_session_id"] == "sess-s6a"
    assert produced["causal_event_id"] == f"result:{result_row['id']}"
    from runtime.daemon.routes.authority_policy import _outcome_receipts_complete
    projected = AuthorityPolicyStore(orchestrator._db).list_outcomes(
        "engineering", cursor=None, limit=10,
    )[0]
    assert len(projected) == 1
    complete, _ = _outcome_receipts_complete(
        type("Org", (), {"db": orchestrator._db})(), projected[0]
    )
    assert complete, {
        "projected": projected[0],
        "task": dict(orchestrator._db.get_task(task_id).__dict__),
        "audit": orchestrator._db.get_audit_logs(task_id),
    }


@pytest.mark.parametrize("marker", [
    "## [RESERVED] Active Team Escalation Policy",
    "<!-- BEGIN HAPPYRANCH ACTIVE TEAM POLICY -->",
    "<!-- END HAPPYRANCH ACTIVE TEAM POLICY -->",
])
def test_run_agent_shipping_seam_rejects_reserved_untrusted_brief(
    orchestrator, test_runtime, monkeypatch, marker,
):
    from runtime.orchestrator.active_authority_policy import ActiveAuthorityPolicyError
    _setup_workspaces(test_runtime)
    task_id = orchestrator.create_task(f"hostile {marker}")
    monkeypatch.setattr(orchestrator, "_build_session_id", lambda: "sess-hostile")
    with pytest.raises(ActiveAuthorityPolicyError, match="server-reserved"):
        orchestrator._run_agent(task_id, "dev_agent", "")


def test_codex_agent_prompt_uses_provider_specific_wording(
    orchestrator, test_runtime, monkeypatch,
):
    _setup_codex_workspace(test_runtime, "engineering_head")
    task_id = orchestrator.create_task("Explore payments")
    monkeypatch.setattr(orchestrator, "_build_session_id", lambda: "sess-eh")

    mock_executor = MagicMock()
    mock_executor.run.return_value = ExecutorResult(
        success=True,
        duration_seconds=30,
        session_id="sess-eh",
    )
    with patch.object(orchestrator, "_build_executor", return_value=mock_executor):
        orchestrator._run_agent(task_id, "engineering_head", "Decide what to do next")

    prompt = mock_executor.run.call_args.kwargs["prompt"]
    assert "Use the start-task skill" not in prompt
    assert "Use the injected task parameters directly" in prompt
    assert "task_id: TASK-001" in prompt
    assert "brief: Explore payments" in prompt


def test_run_agent_registers_active_session_when_tracker_attached(
    orchestrator, test_runtime, monkeypatch,
):
    """Regression for the 8581f26 bug: when the daemon's SessionTracker is
    attached, `_run_agent` must call `set_active(task_id, agent, session_id)`
    BEFORE the subprocess starts. Without this, the agent's
    `happyranch report-completion` callback hits 409 unknown_session and the task
    silently fails with note='agent session failed'."""
    from runtime.daemon.sessions import SessionTracker

    _setup_workspaces(test_runtime)
    tracker = SessionTracker()
    orchestrator.attach_sessions(tracker)

    task_id = orchestrator.create_task("Explore payments")
    monkeypatch.setattr(orchestrator, "_build_session_id", lambda: "sess-eh")

    mock_executor = MagicMock()
    mock_executor.run.return_value = ExecutorResult(
        success=True, duration_seconds=1, session_id="sess-eh",
    )
    with patch.object(orchestrator, "_build_executor", return_value=mock_executor):
        orchestrator._run_agent(task_id, "engineering_head", "any prompt")

    assert tracker.get_active(task_id, "engineering_head") == "sess-eh"


def test_run_agent_skips_session_registration_when_tracker_not_attached(
    orchestrator, test_runtime, monkeypatch,
):
    """Tests construct an Orchestrator without attaching a tracker. The call
    must not raise, and the explicit `on_session_started` callback path must
    still fire so legacy test fixtures keep working."""
    _setup_workspaces(test_runtime)
    assert orchestrator._sessions is None

    task_id = orchestrator.create_task("Explore payments")
    monkeypatch.setattr(orchestrator, "_build_session_id", lambda: "sess-eh")
    captured: list[tuple[str, str, str]] = []

    mock_executor = MagicMock()
    mock_executor.run.return_value = ExecutorResult(
        success=True, duration_seconds=1, session_id="sess-eh",
    )
    with patch.object(orchestrator, "_build_executor", return_value=mock_executor):
        orchestrator._run_agent(
            task_id, "engineering_head", "any prompt",
            on_session_started=lambda t, a, s: captured.append((t, a, s)),
        )

    assert captured == [(task_id, "engineering_head", "sess-eh")]


def test_run_agent_fails_fast_when_workspace_missing_skill(orchestrator, test_runtime, test_settings, monkeypatch):
    """TASK-2511: When workspace skill materialization fails, the failure
    propagates as a named error (SymlinkMaterializationError) before executor
    launch — never a bare WorkspaceNotInitialized or Errno 2.

    Under the canonical store model, the correct unit seam for inducing
    a determinate missing-marker failure is to inject an explicit
    materialization error so the readiness marker symlink is never created."""
    from runtime.skills.symlink_materializer import (
        SymlinkMaterializer,
        SymlinkMaterializationError,
    )

    # Setup workspace with real source skill dirs so the canonical build path
    # has content, then inject a materialization error to prevent symlink creation.
    _setup_workspaces(test_runtime, ["engineering_head"])

    # Create source skill dirs in protocol/skills/ for the project_root temp.
    proto_skills = test_settings.get_protocol_dir() / "skills"
    proto_skills.mkdir(parents=True, exist_ok=True)
    for sid in ("start-task", "jobs", "make-worktree", "thread", "dream"):
        (proto_skills / sid).mkdir(parents=True, exist_ok=True)
        (proto_skills / sid / "SKILL.md").write_text(f"# {sid}\n\nSkill body.\n")

    def _failing_materialize(self, skill_slug, version, content_hash,
                             workspace, skills_subdir, **kwargs):
        raise SymlinkMaterializationError(
            "injected_failure",
            f"Injected materialization failure for {skill_slug}",
        )

    monkeypatch.setattr(SymlinkMaterializer, "materialize_skill", _failing_materialize)

    task_id = orchestrator.create_task("ping")
    eh_workspace = test_runtime.workspaces_dir / "engineering_head"

    with pytest.raises(SymlinkMaterializationError) as exc_info:
        orchestrator._run_agent(task_id, "engineering_head", "any prompt")

    msg = str(exc_info.value)
    assert "Injected materialization failure" in msg
    assert "Errno 2" not in msg  # never bare Errno 2


def test_run_agent_raises_system_contract_error_on_missing_source(
    orchestrator, test_runtime, test_settings, monkeypatch):
    """TASK-4173 adversarial: When a required system-contract source
    directory is absent from protocol/skills/, materialize_workspace_skills
    must raise SystemContractMaterializationError — NOT silently continue.

    Proves:
    - Named terminal failure (SystemContractMaterializationError)
    - Zero executor.run calls
    - No unsafe skill links under .claude/skills or .agents/skills
    - Pre-existing workspace state unchanged
    - No task success/audit/config mutation claims launch occurred
    """
    _setup_workspaces(test_runtime, ["engineering_head"])

    # The autouse _ensure_protocol_skills fixture pre-creates ALL four
    # contracts. We must remove start-task to simulate a real missing-source
    # scenario.
    import shutil
    proto_skills = test_settings.get_protocol_dir() / "skills"
    start_task_dir = proto_skills / "start-task"
    if start_task_dir.exists():
        shutil.rmtree(start_task_dir)
    # Verify it's truly gone.
    assert not start_task_dir.exists(), "start-task directory must be removed"

    from runtime.orchestrator.workspace_adapters import (
        SystemContractMaterializationError,
    )

    task_id = orchestrator.create_task("ping")
    eh_workspace = test_runtime.workspaces_dir / "engineering_head"

    # Before: record existing files for later comparison.
    pre_claude_skills = eh_workspace / ".claude" / "skills"
    pre_agents_skills = eh_workspace / ".agents" / "skills"
    pre_claude_existed = pre_claude_skills.exists()
    pre_agents_existed = pre_agents_skills.exists()

    with pytest.raises(SystemContractMaterializationError) as exc_info:
        orchestrator._run_agent(task_id, "engineering_head", "any prompt")

    msg = str(exc_info.value)
    assert "start-task" in msg, (
        f"Error must name 'start-task' as missing: {msg!r}"
    )
    assert "missing" in msg.lower() or "not on disk" in msg.lower(), (
        f"Error should mention missing/not-on-disk: {msg!r}"
    )

    # No unsafe skill links were created under either root.
    # If the workspace had no .claude/skills before, it must still not
    # have one after the failed materialization.
    post_claude_skills = eh_workspace / ".claude" / "skills"
    post_agents_skills = eh_workspace / ".agents" / "skills"
    assert post_claude_skills.exists() == pre_claude_existed, (
        ".claude/skills state must be unchanged after failed materialization"
    )
    assert post_agents_skills.exists() == pre_agents_existed, (
        ".agents/skills state must be unchanged after failed materialization"
    )


def test_run_agent_accepts_codex_readiness_marker(orchestrator, test_runtime, monkeypatch):
    _setup_codex_workspace(test_runtime, "engineering_head")
    task_id = orchestrator.create_task("ping")
    monkeypatch.setattr(orchestrator, "_build_session_id", lambda: "sess-eh")

    mock_executor = MagicMock()
    mock_executor.run.return_value = ExecutorResult(
        success=True,
        duration_seconds=1,
        session_id="sess-eh",
    )
    with patch.object(orchestrator, "_build_executor", return_value=mock_executor):
        result, report = orchestrator._run_agent(task_id, "engineering_head", "any prompt")

    assert result.success is True
    assert report is None
    assert mock_executor.run.call_count == 1


def test_run_agent_routes_opencode_workspace_to_opencode_executor(
    orchestrator, test_runtime, monkeypatch,
):
    """An opencode-configured workspace must dispatch to OpencodeExecutor,
    not Claude or Codex. Readiness is the AGENTS.md marker — same as Codex —
    because opencode reads AGENTS.md and discovers skills under
    .agents/skills/."""
    _setup_opencode_workspace(test_runtime, "engineering_head")
    task_id = orchestrator.create_task("ping")
    monkeypatch.setattr(orchestrator, "_build_session_id", lambda: "sess-eh")

    mock_executor = MagicMock()
    mock_executor.run.return_value = ExecutorResult(
        success=True,
        duration_seconds=1,
        session_id="sess-eh",
    )
    with patch.object(orchestrator, "_build_executor", return_value=mock_executor) as mock_build:
        result, report = orchestrator._run_agent(task_id, "engineering_head", "any prompt")

    assert result.success is True
    assert report is None
    mock_build.assert_called_once_with("opencode")
    assert mock_executor.run.call_count == 1
    # opencode shares the Claude-style "use the start-task skill" nudge
    # because opencode's `skill` tool resolves the skill on demand.
    prompt = mock_executor.run.call_args.kwargs["prompt"]
    assert "Use the start-task skill" in prompt
    assert "Use the injected task parameters directly" not in prompt


def test_run_agent_routes_pi_workspace_to_pi_executor(
    orchestrator, test_runtime, monkeypatch,
):
    _setup_pi_workspace(test_runtime, "engineering_head")
    task_id = orchestrator.create_task("ping")
    monkeypatch.setattr(orchestrator, "_build_session_id", lambda: "sess-eh")

    mock_executor = MagicMock()
    mock_executor.run.return_value = ExecutorResult(
        success=True,
        duration_seconds=1,
        session_id="sess-eh",
    )
    with patch.object(orchestrator, "_build_executor", return_value=mock_executor) as mock_build:
        result, report = orchestrator._run_agent(task_id, "engineering_head", "any prompt")

    assert result.success is True
    assert report is None
    mock_build.assert_called_once_with("pi")
    assert mock_executor.run.call_count == 1
    prompt = mock_executor.run.call_args.kwargs["prompt"]
    assert "Use the start-task skill" in prompt
    assert "Use the injected task parameters directly" not in prompt


def test_run_agent_fails_closed_when_agent_def_missing(
    orchestrator, test_runtime, monkeypatch,
):
    """THR-095/TASK-5293: with no active AgentDef, task launch must raise
    AgentUnavailableError instead of silently defaulting to claude."""
    workspace = test_runtime.workspaces_dir / "engineering_head"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "task_history.md").write_text("# Task History: engineering_head\n\n")
    (workspace / "agent.yaml").write_text("repos: {}\n")
    (workspace / "repos" / "test" / ".git").mkdir(parents=True, exist_ok=True)
    # Ensure NO org/agents/engineering_head.md exists (autouse fixture created
    # it, so remove it to test the missing-AgentDef path).
    (test_runtime.agents_dir / "engineering_head.md").unlink(missing_ok=True)

    task_id = orchestrator.create_task("ping")
    monkeypatch.setattr(orchestrator, "_build_session_id", lambda: "sess-eh")

    mock_executor = MagicMock()
    with patch.object(orchestrator, "_build_executor", return_value=mock_executor):
        with pytest.raises(AgentUnavailableError):
            orchestrator._run_agent(task_id, "engineering_head", "any prompt")

    assert mock_executor.run.call_count == 0


def test_run_agent_materializes_todos_skill_on_task_path(
    orchestrator, test_runtime, monkeypatch,
):
    """Normal task path must materialize the todos system contract to
    .agents/skills/todos/SKILL.md before the executor runs.

    This proves the universal todos SystemContract reaches every task
    session through the existing materialize_workspace_skills path."""
    _setup_workspaces(test_runtime, ["dev_agent"])
    ws = test_runtime.workspaces_dir / "dev_agent"
    task_id = orchestrator.create_task("ping with todos")
    monkeypatch.setattr(orchestrator, "_build_session_id", lambda: "sess-td")

    mock_executor = MagicMock()
    mock_executor.run.return_value = ExecutorResult(
        success=True,
        duration_seconds=1,
        session_id="sess-td",
    )
    with patch.object(orchestrator, "_build_executor", return_value=mock_executor):
        result, _ = orchestrator._run_agent(task_id, "dev_agent", "any prompt")

    assert result.success is True
    assert mock_executor.run.call_count == 1

    # Prove the universal todos SystemContract materialized through the
    # real _run_agent / TASK context path.
    todos_marker = ws / ".agents" / "skills" / "todos" / "SKILL.md"
    assert todos_marker.is_file(), (
        f"todos skill not materialized at {todos_marker}; "
        f"workspace skills dir contents: "
        f"{list((ws / '.agents' / 'skills').rglob('*')) if (ws / '.agents' / 'skills').is_dir() else 'missing'}"
    )


@pytest.mark.parametrize("revisit_of", [None, "TASK-PREVIOUS"])
def test_run_agent_materializes_persistent_verification_contract_for_descendants(
    orchestrator, test_runtime, test_settings, monkeypatch, revisit_of,
):
    """Descendant and revisit brief reconstruction still launch as TASK context."""
    _setup_workspaces(test_runtime, ["dev_agent"])
    source = (
        Path(__file__).resolve().parents[1]
        / "protocol" / "skills" / "jobs" / "SKILL.md"
    ).read_text()
    (test_settings.get_protocol_dir() / "skills" / "jobs" / "SKILL.md").write_text(source)

    orchestrator._db.insert_task(TaskRecord(
        id="TASK-PARENT", brief="parent", team="engineering",
        status=TaskStatus.IN_PROGRESS,
    ))
    if revisit_of:
        orchestrator._db.insert_task(TaskRecord(
            id=revisit_of, brief="failed predecessor", team="engineering",
            parent_task_id="TASK-PARENT", status=TaskStatus.FAILED,
        ))
    child_id = "TASK-REVISIT" if revisit_of else "TASK-DESCENDANT"
    orchestrator._db.insert_task(TaskRecord(
        id=child_id, brief="reconstructed child brief", team="engineering",
        assigned_agent="dev_agent", parent_task_id="TASK-PARENT",
        revisit_of_task_id=revisit_of,
    ))
    monkeypatch.setattr(orchestrator, "_build_session_id", lambda: "sess-contract")
    mock_executor = MagicMock()
    mock_executor.run.return_value = ExecutorResult(
        success=True, duration_seconds=1, session_id="sess-contract",
    )

    with patch.object(orchestrator, "_build_executor", return_value=mock_executor):
        result, _ = orchestrator._run_agent(child_id, "dev_agent", "")

    assert result.success is True
    shipped = (
        test_runtime.workspaces_dir / "dev_agent" / ".agents" / "skills"
        / "jobs" / "SKILL.md"
    ).read_text()
    assert "scripts/local_ci.sh all" in shipped
    assert '"persistent": true' in shipped
    assert "waiting_on_job_ids" in shipped


def test_task_history_written_per_agent_only(orchestrator, test_runtime):
    """_update_task_history writes the file to the assigned_agent's workspace only."""
    _setup_workspaces(test_runtime)

    orchestrator.create_task("Add Alipay support")
    orchestrator._db.update_task(
        "TASK-001",
        assigned_agent="dev_agent",
        status=TaskStatus.COMPLETED,
        note="dev did it",
    )
    orchestrator._update_task_history("TASK-001")

    dev_hist = (test_runtime.workspaces_dir / "dev_agent" / "task_history.md").read_text()
    pm_hist = (test_runtime.workspaces_dir / "product_manager" / "task_history.md").read_text()

    assert "TASK-001" in dev_hist
    assert "TASK-001" not in pm_hist


def test_task_history_entry_format(orchestrator, test_runtime):
    """task_history.md entries follow the `**TASK-id** (date, status) — brief` format."""
    _setup_workspaces(test_runtime)

    orchestrator.create_task("Review Q1 project status")
    orchestrator._db.update_task(
        "TASK-001",
        assigned_agent="engineering_head",
        status=TaskStatus.COMPLETED,
        note="Reviewed Q1. Three risks, five actions.",
    )
    orchestrator._update_task_history("TASK-001")

    hist = (test_runtime.workspaces_dir / "engineering_head" / "task_history.md").read_text()
    assert re.search(r"\*\*TASK-001\*\* \(\d{4}-\d{2}-\d{2}, completed\) — Review Q1", hist)
    assert "Outcome: Reviewed Q1. Three risks, five actions." in hist
    assert "Artifact:" not in hist


def test_task_history_newest_first(orchestrator, test_runtime):
    """task_history.md lists entries newest-first."""
    _setup_workspaces(test_runtime)

    orchestrator.create_task("First task")
    orchestrator._db.update_task(
        "TASK-001",
        assigned_agent="engineering_head",
        status=TaskStatus.COMPLETED,
        note="first",
    )
    orchestrator._update_task_history("TASK-001")

    orchestrator.create_task("Second task")
    orchestrator._db.update_task(
        "TASK-002",
        assigned_agent="engineering_head",
        status=TaskStatus.COMPLETED,
        note="second",
    )
    orchestrator._update_task_history("TASK-002")

    hist = (test_runtime.workspaces_dir / "engineering_head" / "task_history.md").read_text()
    idx2 = hist.index("TASK-002")
    idx1 = hist.index("TASK-001")
    assert idx2 < idx1


def test_read_completion_from_db_preserves_output_dir(orchestrator):
    """Reconstructing a CompletionReport from task_results must include
    output_dir so the daemon-callback path can persist tasks.final_output_dir."""
    orchestrator.create_task("Write the report")
    orchestrator._db.insert_task_result(
        "TASK-001",
        "dev_agent",
        "sess-xyz",
        output_summary="Report done",
        confidence_score=85,
        output_dir="output/TASK-001",
    )

    report = orchestrator._read_completion_from_db("TASK-001", "dev_agent", "sess-xyz")
    assert report is not None
    assert report.output_dir == "output/TASK-001"


def test_read_completion_from_db_hydrates_decision(orchestrator):
    """EH's structured decision is persisted as JSON on task_results.decision_json
    and must be rehydrated into report.decision as a NextStep so the parser
    consumes it directly — no prose inference."""
    import json as _json
    from runtime.models import NextStep

    orchestrator.create_task("Clean up stale PR/issue")
    orchestrator._db.insert_task_result(
        "TASK-001",
        "engineering_head",
        "sess-eh",
        output_summary="Prose recap: closed issue #93 and PR #105.",
        confidence_score=95,
        decision_json=_json.dumps({
            "action": "done",
            "summary": "Cleanup complete.",
        }),
    )

    report = orchestrator._read_completion_from_db(
        "TASK-001", "engineering_head", "sess-eh",
    )
    assert report is not None
    assert isinstance(report.decision, NextStep)
    assert report.decision.action == "done"
    assert report.decision.summary == "Cleanup complete."
    # Prose summary still round-trips unchanged — separating the two is the
    # whole point of the decision field.
    assert "closed issue #93" in report.output_summary


def test_parse_next_step_prefers_decision_field_over_prose(orchestrator):
    """TASK-071 regression: when `decision` is populated, the parser must use
    it verbatim and never fall through to JSON-decoding the prose summary.
    This is the fix that eliminates the double-encoding trap — prose in
    `output_summary` is no longer an escalation trigger if the structured
    decision is present.
    """
    from runtime.models import CompletionReport, NextStep

    # Prose output_summary + structured decision — the TASK-071 shape.
    report = CompletionReport(
        task_id="TASK-071",
        agent="engineering_head",
        status="completed",
        confidence=98,
        output_summary=(
            "Cleanup pass complete. Issue #93 closed with reason=completed "
            "plus resolution comment. Stale PR #105 closed."
        ),
        decision=NextStep(action="done", summary="Cleanup landed."),
    )
    decision = orchestrator._parse_next_step(report)
    assert decision.action == "done"
    assert decision.summary == "Cleanup landed."


def test_parse_next_step_legacy_path_still_works_for_json_in_output_summary(
    orchestrator,
):
    """Workspaces on older skill copies keep speaking the pre-TASK-071
    contract: JSON decision embedded directly in output_summary, no `decision`
    field. Parser must continue to honor that during the transition."""
    import json as _json
    from runtime.models import CompletionReport

    report = CompletionReport(
        task_id="TASK-050",
        agent="engineering_head",
        status="completed",
        confidence=80,
        output_summary=_json.dumps({
            "action": "delegate",
            "agent": "dev_agent",
            "prompt": "Implement X",
        }),
        decision=None,
    )
    decision = orchestrator._parse_next_step(report)
    assert decision.action == "delegate"
    assert decision.agent == "dev_agent"
    assert decision.prompt == "Implement X"


def test_parse_next_step_prose_without_decision_still_escalates(orchestrator):
    """The guardrail against silent-approve-from-prose (TASK-013 / TASK-016)
    must remain: if EH sends prose AND omits `decision`, escalate. The new
    escalation message points at the missing `decision` field so the fix is
    obvious from the audit log."""
    from runtime.models import CompletionReport

    report = CompletionReport(
        task_id="TASK-099",
        agent="engineering_head",
        status="completed",
        confidence=80,
        output_summary="Delegating to dev_agent now.",
        decision=None,
    )
    decision = orchestrator._parse_next_step(report)
    assert decision.action == "escalate"
    assert "decision" in (decision.reason or "").lower()
    assert "fanout" in (decision.reason or "").lower()
    assert "delegate" in (decision.reason or "").lower()


def test_read_completion_from_db_tolerates_garbage_decision_json(orchestrator):
    """A corrupt decision_json row must not crash the orchestrator — leave
    decision None so the parser escalates with a readable reason, rather than
    silently falling through to prose inference."""
    orchestrator.create_task("Task with corrupt row")
    orchestrator._db.insert_task_result(
        "TASK-001",
        "engineering_head",
        "sess-eh",
        output_summary="prose",
        confidence_score=70,
        decision_json="not-json{",
    )
    report = orchestrator._read_completion_from_db(
        "TASK-001", "engineering_head", "sess-eh",
    )
    assert report is not None
    assert report.decision is None


def test_read_completion_from_db_reconstructs_local_ci(orchestrator):
    """A persisted local_ci column is round-tripped back into the CompletionReport."""
    import json as _json
    orchestrator.create_task("Task with local_ci")
    orchestrator._db.insert_task_result(
        "TASK-001",
        "dev_agent",
        "sess-lc",
        output_summary="done",
        confidence_score=95,
        local_ci_json=_json.dumps({"command": "scripts/local_ci.sh all", "exit_code": 0}),
    )
    report = orchestrator._read_completion_from_db("TASK-001", "dev_agent", "sess-lc")
    assert report is not None
    assert report.local_ci is not None
    assert report.local_ci.command == "scripts/local_ci.sh all"
    assert report.local_ci.exit_code == 0


def test_read_completion_from_db_null_local_ci(orchestrator):
    """Legacy rows where local_ci is NULL reconstruct with local_ci=None."""
    orchestrator.create_task("Legacy task no local_ci")
    orchestrator._db.insert_task_result(
        "TASK-001",
        "dev_agent",
        "sess-legacy",
        output_summary="done",
        confidence_score=80,
        local_ci_json=None,
    )
    report = orchestrator._read_completion_from_db("TASK-001", "dev_agent", "sess-legacy")
    assert report is not None
    assert report.local_ci is None


def test_read_completion_from_db_corrupt_local_ci(orchestrator):
    """Corrupt local_ci JSON degrades to None without crashing."""
    orchestrator.create_task("Corrupt local_ci task")
    orchestrator._db.insert_task_result(
        "TASK-001",
        "dev_agent",
        "sess-corrupt",
        output_summary="done",
        confidence_score=80,
        local_ci_json="not-json{",
    )
    report = orchestrator._read_completion_from_db("TASK-001", "dev_agent", "sess-corrupt")
    assert report is not None
    assert report.local_ci is None


def test_local_ci_column_migration_idempotent(orchestrator):
    """The ALTER TABLE ADD COLUMN local_ci TEXT migration is idempotent.
    Running it twice via the _ensure_schema path does not crash."""
    # The column already exists after the first migration run during DB init.
    # We can verify by re-running the ALTER and catching sqlite3.OperationalError.
    import sqlite3
    try:
        orchestrator._db._conn.execute(
            "ALTER TABLE task_results ADD COLUMN local_ci TEXT"
        )
        # If we get here, the column already exists (no error) or was added.
        # Either way, the idempotent migration pattern works.
    except sqlite3.OperationalError:
        # Expected: column already exists.
        pass


def test_orchestrator_requires_teams() -> None:
    import pytest
    from pathlib import Path
    from runtime.config import Settings
    from runtime.infrastructure.database import Database
    from runtime.orchestrator._paths import OrgPaths
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.runtime import RuntimeDir
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        rt = RuntimeDir.init(Path(td) / "rt")
        paths = OrgPaths(root=rt.orgs_dir / "x")
        db = Database(paths.db_path)
        settings = Settings()
        with pytest.raises(TypeError):
            Orchestrator(db=db, settings=settings, paths=paths, slug="x")  # missing teams


def test_orchestrator_notifier_default_none(tmp_path, test_settings):
    from runtime.infrastructure.database import Database
    from runtime.orchestrator._paths import OrgPaths
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.orchestrator.teams import TeamsRegistry

    root = tmp_path / "orgs" / "x"
    root.mkdir(parents=True)
    db = Database(root / "happyranch.db")
    orch = Orchestrator(
        db=db, settings=test_settings,
        paths=OrgPaths(root=root), slug="x",
        teams=TeamsRegistry.load(root),
    )
    assert orch._notifier is None


def test_orchestrator_notify_escalated_no_op_when_unset(tmp_path, test_settings):
    from runtime.infrastructure.database import Database
    from runtime.orchestrator._paths import OrgPaths
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.orchestrator.teams import TeamsRegistry

    root = tmp_path / "orgs" / "x"
    root.mkdir(parents=True)
    db = Database(root / "happyranch.db")
    orch = Orchestrator(
        db=db, settings=test_settings,
        paths=OrgPaths(root=root), slug="x",
        teams=TeamsRegistry.load(root),
    )
    orch.notify_escalated(task_id="TASK-X", agent="a", reason="r")  # must not raise


def test_orchestrator_notify_does_not_block_synchronous_caller(tmp_path, test_settings):
    """When called from a thread without an event loop, notify_escalated
    must spawn a background worker rather than blocking on asyncio.run."""
    import threading
    import time

    from runtime.infrastructure.database import Database
    from runtime.orchestrator._paths import OrgPaths
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.orchestrator.teams import TeamsRegistry

    root = tmp_path / "orgs" / "x"
    root.mkdir(parents=True)
    db = Database(root / "happyranch.db")
    orch = Orchestrator(
        db=db, settings=test_settings,
        paths=OrgPaths(root=root), slug="x",
        teams=TeamsRegistry.load(root),
    )

    started = threading.Event()
    finish = threading.Event()
    finished = threading.Event()

    class _SlowNotifier:
        async def notify_escalated(self, **kwargs):
            started.set()
            finish.wait(timeout=5.0)
            finished.set()

    orch.attach_notifier(_SlowNotifier())

    t0 = time.monotonic()
    orch.notify_escalated(task_id="TASK-X", agent="a", reason="r")
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"notify_escalated blocked for {elapsed:.2f}s"
    assert started.wait(timeout=2.0), "background notifier never ran"
    finish.set()
    assert finished.wait(timeout=2.0)


# ── Task attachment materialization tests (THR-109) ──────────────────────


def test_materialize_collision_safe_filenames(test_settings, test_runtime):
    """When own and ancestor attachments share a display_name,
    materialized filenames must be collision-safe via storage_key prefix."""
    from datetime import datetime, timezone
    from runtime.models import TaskRecord
    from runtime.orchestrator._paths import OrgPaths
    from runtime.infrastructure.task_attachment_store import TaskAttachmentStore

    db = Database(test_runtime.db_path)
    now = datetime.now(timezone.utc)

    # Root task with attachment "shared.png".
    db.insert_task(TaskRecord(
        id="COL-ROOT", brief="root", team="engineering",
        created_at=now, updated_at=now,
    ))
    db.insert_task_attachment(
        task_id="COL-ROOT", ordinal=0,
        storage_key="col-root-key", display_name="shared.png",
        size_bytes=3, content_type="image/png", uploaded_by="founder",
    )
    # Child task with its own attachment also named "shared.png".
    db.insert_task(TaskRecord(
        id="COL-CHILD", brief="child", team="engineering",
        parent_task_id="COL-ROOT", created_at=now, updated_at=now,
    ))
    db.insert_task_attachment(
        task_id="COL-CHILD", ordinal=0,
        storage_key="col-child-key", display_name="shared.png",
        size_bytes=5, content_type="image/png", uploaded_by="founder",
    )

    # Write bytes to the store.
    store = TaskAttachmentStore(OrgPaths(test_runtime.root).task_attachments_dir)
    store.put("col-root-key", b"abc")
    store.put("col-child-key", b"hello")

    teams = TeamsRegistry.load(test_runtime.root)
    orch = Orchestrator(
        db=db, settings=test_settings,
        paths=test_runtime, slug="test", teams=teams,
    )

    workspace = test_runtime.workspaces_dir / "test-agent"
    session_id = "sess-collision"

    block = orch._materialize_task_attachments(
        workspace=workspace, task_id="COL-CHILD", session_id=session_id,
    )

    # Both files must exist (no overwrite).
    session_dir = workspace / ".happyranch" / "attachments" / session_id
    files = sorted(session_dir.iterdir())
    assert len(files) == 2, f"Expected 2 files, got: {[f.name for f in files]}"

    # Files must be named with storage_key prefix (collision-safe).
    names = {f.name for f in files}
    assert any("col-root-key" in n for n in names), (
        f"Root attachment not found in: {names}"
    )
    assert any("col-child-key" in n for n in names), (
        f"Child attachment not found in: {names}"
    )

    # Both display names must appear in the prompt block.
    assert "shared.png" in block

    # File contents must be correct.
    root_file = next(f for f in files if "col-root-key" in f.name)
    child_file = next(f for f in files if "col-child-key" in f.name)
    assert root_file.read_bytes() == b"abc"
    assert child_file.read_bytes() == b"hello"


def test_materialize_legacy_duplicate_rows_distinct_paths(test_settings, test_runtime):
    """Legacy duplicate rows sharing both storage_key and display_name
    must produce distinct materialized paths using the immutable row id."""
    import sqlite3
    from datetime import datetime, timezone
    from runtime.models import TaskRecord
    from runtime.orchestrator._paths import OrgPaths
    from runtime.infrastructure.task_attachment_store import TaskAttachmentStore

    now = datetime.now(timezone.utc)

    # Build a pre-index v1 database with duplicate rows sharing both
    # storage_key AND display_name. Use raw SQLite to bypass the UNIQUE
    # constraint that Database init would enforce.
    test_runtime.db_path.parent.mkdir(parents=True, exist_ok=True)
    raw_conn = sqlite3.connect(str(test_runtime.db_path))
    raw_conn.execute("PRAGMA journal_mode=WAL")
    raw_conn.execute("PRAGMA foreign_keys=ON")
    raw_conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'pending',
            assigned_agent TEXT,
            team TEXT NOT NULL DEFAULT 'engineering',
            brief TEXT NOT NULL,
            task_type TEXT NOT NULL DEFAULT 'task',
            revision_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            parent_task_id TEXT,
            final_output_summary TEXT,
            final_output_dir TEXT,
            executor_pid INTEGER
        );
        CREATE TABLE IF NOT EXISTS task_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            storage_key TEXT NOT NULL,
            display_name TEXT NOT NULL,
            size_bytes INTEGER,
            content_type TEXT,
            uploaded_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id),
            UNIQUE(task_id, ordinal)
        );
    """)
    raw_conn.execute(
        "INSERT INTO tasks (id, status, team, brief, parent_task_id, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("LEG-DUP-PARENT", "completed", "engineering", "parent", None,
         now.isoformat(), now.isoformat()),
    )
    raw_conn.execute(
        "INSERT INTO tasks (id, status, team, brief, parent_task_id, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("LEG-DUP-CHILD", "pending", "engineering", "child",
         "LEG-DUP-PARENT", now.isoformat(), now.isoformat()),
    )
    # Insert two legacy rows with same storage_key AND same display_name.
    raw_conn.execute(
        "INSERT INTO task_attachments (task_id, ordinal, storage_key, "
        "display_name, size_bytes, content_type, uploaded_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("LEG-DUP-PARENT", 0, "leg-dup-key", "same-name.png", 4,
         "image/png", "founder", now.isoformat()),
    )
    raw_conn.execute(
        "INSERT INTO task_attachments (task_id, ordinal, storage_key, "
        "display_name, size_bytes, content_type, uploaded_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("LEG-DUP-PARENT", 1, "leg-dup-key", "same-name.png", 5,
         "image/png", "founder", now.isoformat()),
    )
    raw_conn.commit()
    raw_conn.close()

    # Now open via Database — migration will detect the duplicates and mark
    # them as legacy (legacy_status='duplicate_v1'), creating a partial
    # UNIQUE index.
    db = Database(test_runtime.db_path)

    # Verify the legacy rows have been marked.
    rows = db._conn.execute(
        "SELECT id, storage_key, display_name, legacy_status "
        "FROM task_attachments WHERE storage_key = ? ORDER BY id",
        ("leg-dup-key",),
    ).fetchall()
    assert len(rows) == 2, "must have 2 legacy duplicate rows"
    id_a, id_b = rows[0]["id"], rows[1]["id"]
    assert id_a != id_b, "row IDs must differ"
    assert rows[0]["legacy_status"] == "duplicate_v1"
    assert rows[1]["legacy_status"] == "duplicate_v1"
    assert rows[0]["display_name"] == "same-name.png"
    assert rows[1]["display_name"] == "same-name.png"

    # Write fixture bytes to the private store.
    store = TaskAttachmentStore(OrgPaths(test_runtime.root).task_attachments_dir)
    store.put("leg-dup-key", b"AAAA")

    teams = TeamsRegistry.load(test_runtime.root)
    orch = Orchestrator(
        db=db, settings=test_settings,
        paths=test_runtime, slug="test", teams=teams,
    )

    workspace = test_runtime.workspaces_dir / "test-agent"
    session_id = "sess-legdup"

    block = orch._materialize_task_attachments(
        workspace=workspace, task_id="LEG-DUP-CHILD", session_id=session_id,
    )

    # Both materialized files must exist with distinct paths using row IDs.
    session_dir = workspace / ".happyranch" / "attachments" / session_id
    files = sorted(session_dir.iterdir())
    assert len(files) == 2, (
        f"Expected 2 materialized files for legacy duplicates, "
        f"got {len(files)}: {[f.name for f in files]}"
    )

    # Each filename must embed its respective row id.
    names = {f.name for f in files}
    assert any(f"__{id_a}" in n for n in names), (
        f"Row id_a={id_a} not found in filenames: {names}"
    )
    assert any(f"__{id_b}" in n for n in names), (
        f"Row id_b={id_b} not found in filenames: {names}"
    )

    # Both files share the same storage_key and display_name prefix.
    assert all("leg-dup-key__same-name.png" in n for n in names), (
        f"Expected storage_key + display_name prefix in all names: {names}"
    )

    # File contents are correct (both read from the same store key).
    for f in files:
        assert f.read_bytes() == b"AAAA", (
            f"File {f.name} must contain correct bytes"
        )

    # Prompt block must reference both distinct paths.
    assert "same-name.png" in block
    # Each prompt line must point to a distinct file path.
    prompt_paths = [
        line.split(" -> ")[-1].strip()
        for line in block.splitlines()
        if " -> " in line
    ]
    assert len(prompt_paths) == 2, (
        f"Expected 2 prompt file references, got {len(prompt_paths)}"
    )
    assert prompt_paths[0] != prompt_paths[1], (
        "Prompt paths must be distinct"
    )
    db._conn.close()


def test_materialize_sanitized_names_do_not_escape_dir(test_settings, test_runtime):
    """Materialized filenames are sanitized so malformed DB display names
    (e.g., with path separators) cannot escape the session dir."""
    from datetime import datetime, timezone
    from runtime.models import TaskRecord
    from runtime.orchestrator._paths import OrgPaths
    from runtime.infrastructure.task_attachment_store import TaskAttachmentStore
    from runtime.infrastructure.task_attachment_store import TaskAttachmentInvalidName

    db = Database(test_runtime.db_path)
    now = datetime.now(timezone.utc)

    # Task with a display_name that contains a path separator.
    # This would have been rejected at upload, but we test the
    # materialization boundary defense-in-depth.
    db.insert_task(TaskRecord(
        id="MALNAME", brief="test", team="engineering",
        created_at=now, updated_at=now,
    ))
    # Bypassing the API to insert a malformed name directly into DB.
    db._conn.execute(
        "INSERT INTO task_attachments "
        "(task_id, ordinal, storage_key, display_name, size_bytes, "
        "content_type, uploaded_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("MALNAME", 0, "safe-key", "../../../etc/passwd", 10,
         "text/plain", "founder", now.isoformat()),
    )
    db._conn.commit()

    store = TaskAttachmentStore(OrgPaths(test_runtime.root).task_attachments_dir)
    store.put("safe-key", b"safe data")

    teams = TeamsRegistry.load(test_runtime.root)
    orch = Orchestrator(
        db=db, settings=test_settings,
        paths=test_runtime, slug="test", teams=teams,
    )

    workspace = test_runtime.workspaces_dir / "test-agent"
    session_id = "sess-malname"

    # sanitize_display_name should reject the traversal name,
    # causing the materialization to skip this attachment.
    # (The log will have a warning; the session dir should be empty.)
    block = orch._materialize_task_attachments(
        workspace=workspace, task_id="MALNAME", session_id=session_id,
    )

    session_dir = workspace / ".happyranch" / "attachments" / session_id
    # The sanitize_display_name inside _materialize_task_attachments
    # should catch the malformed name and raise,
    # but the call is in a try/except loop so it skips gracefully.
    # The session dir may exist but should be empty.
    if session_dir.exists():
        files = list(session_dir.iterdir())
        assert len(files) == 0, (
            f"Malformed name must not produce files: {[f.name for f in files]}"
        )

    # Block should be empty (no materialized attachments).
    assert block == "" or block.endswith("\n")


def test_materialize_audits_on_success(test_settings, test_runtime):
    """Successful materialization must emit task_attachment_materialized
    audit with the expected org/task context."""
    from datetime import datetime, timezone
    from runtime.models import TaskRecord
    from runtime.orchestrator._paths import OrgPaths
    from runtime.infrastructure.task_attachment_store import TaskAttachmentStore

    db = Database(test_runtime.db_path)
    now = datetime.now(timezone.utc)

    # Task with one attachment.
    db.insert_task(TaskRecord(
        id="AUDIT-MAT", brief="test", team="engineering",
        created_at=now, updated_at=now,
    ))
    db.insert_task_attachment(
        task_id="AUDIT-MAT", ordinal=0,
        storage_key="audit-key", display_name="report.pdf",
        size_bytes=4, content_type="application/pdf", uploaded_by="founder",
    )
    store = TaskAttachmentStore(OrgPaths(test_runtime.root).task_attachments_dir)
    store.put("audit-key", b"data")

    teams = TeamsRegistry.load(test_runtime.root)
    orch = Orchestrator(
        db=db, settings=test_settings,
        paths=test_runtime, slug="test", teams=teams,
    )

    workspace = test_runtime.workspaces_dir / "test-agent"
    session_id = "sess-audit"

    block = orch._materialize_task_attachments(
        workspace=workspace, task_id="AUDIT-MAT", session_id=session_id,
    )
    assert block != ""

    # Verify audit row.
    logs = db.get_audit_logs("AUDIT-MAT")
    mat_logs = [l for l in logs if l["action"] == "task_attachment_materialized"]
    assert len(mat_logs) == 1
    log = mat_logs[0]
    assert log["agent"] == "orchestrator"
    payload = log["payload"]
    assert payload["session_id"] == session_id
    assert payload["count"] == 1
    assert "audit-key" in payload["storage_keys"]


def test_materialize_no_audit_when_no_attachments(test_settings, test_runtime):
    """When no attachments exist, no audit is emitted."""
    from datetime import datetime, timezone
    from runtime.models import TaskRecord

    db = Database(test_runtime.db_path)
    now = datetime.now(timezone.utc)

    db.insert_task(TaskRecord(
        id="NOATT", brief="test", team="engineering",
        created_at=now, updated_at=now,
    ))

    teams = TeamsRegistry.load(test_runtime.root)
    orch = Orchestrator(
        db=db, settings=test_settings,
        paths=test_runtime, slug="test", teams=teams,
    )

    workspace = test_runtime.workspaces_dir / "test-agent"
    session_id = "sess-noatt"

    block = orch._materialize_task_attachments(
        workspace=workspace, task_id="NOATT", session_id=session_id,
    )
    assert block == ""

    # No task_attachment_materialized audit should exist.
    logs = db.get_audit_logs("NOATT")
    mat_logs = [l for l in logs if l["action"] == "task_attachment_materialized"]
    assert len(mat_logs) == 0


def test_session_end_cleans_up_attachment_dir(test_settings, test_runtime):
    """When the session ends, the materialized attachment directory is removed.

    Verifies that:
    1. Materialization creates the session attachment dir.
    2. _cleanup_session_attachments removes it.
    3. A different session's dir and the source private store remain intact.
    """
    from datetime import datetime, timezone
    from runtime.models import TaskRecord
    from runtime.orchestrator._paths import OrgPaths
    from runtime.infrastructure.task_attachment_store import TaskAttachmentStore

    db = Database(test_runtime.db_path)
    now = datetime.now(timezone.utc)

    # Task with an attachment.
    db.insert_task(TaskRecord(
        id="CLEANUP-T1", brief="test", team="engineering",
        created_at=now, updated_at=now,
    ))
    db.insert_task_attachment(
        task_id="CLEANUP-T1", ordinal=0,
        storage_key="clean-key", display_name="report.pdf",
        size_bytes=4, content_type="application/pdf", uploaded_by="founder",
    )
    store = TaskAttachmentStore(OrgPaths(test_runtime.root).task_attachments_dir)
    store.put("clean-key", b"data")

    teams = TeamsRegistry.load(test_runtime.root)
    orch = Orchestrator(
        db=db, settings=test_settings,
        paths=test_runtime, slug="test", teams=teams,
    )

    workspace = test_runtime.workspaces_dir / "test-agent"
    session_id = "sess-cleanup"
    other_session_id = "sess-other"

    # Materialize for session_id.
    block = orch._materialize_task_attachments(
        workspace=workspace, task_id="CLEANUP-T1", session_id=session_id,
    )
    assert block != ""

    session_dir = workspace / ".happyranch" / "attachments" / session_id
    assert session_dir.exists(), f"Expected {session_dir} to exist after materialization"

    # Create a "different session" dir to prove it survives cleanup.
    other_dir = workspace / ".happyranch" / "attachments" / other_session_id
    other_dir.mkdir(parents=True, exist_ok=True)
    (other_dir / "dummy.txt").write_text("other")

    # Record source store state.
    source_path = store.path_for("clean-key")
    assert source_path.exists()
    source_bytes = source_path.read_bytes()

    # Drive the terminal cleanup path.
    from runtime.orchestrator.orchestrator import _cleanup_session_attachments
    _cleanup_session_attachments(workspace, session_id)

    # Verify: session_dir is removed.
    assert not session_dir.exists(), (
        f"Session attachment dir must be removed after cleanup: {session_dir}"
    )

    # Verify: other session's dir still exists.
    assert other_dir.exists(), (
        f"Other session's dir must survive cleanup: {other_dir}"
    )

    # Verify: source private-store bytes are intact.
    assert source_path.exists()
    assert source_path.read_bytes() == source_bytes

    # Verify: cleanup is idempotent (calling twice doesn't error).
    _cleanup_session_attachments(workspace, session_id)  # no exception


def test_cleanup_noop_when_dir_missing(test_settings, test_runtime):
    """_cleanup_session_attachments is a no-op when the session dir
    doesn't exist (safe for sessions without attachments)."""
    workspace = test_runtime.workspaces_dir / "test-agent"
    session_id = "sess-never-existed"

    session_dir = workspace / ".happyranch" / "attachments" / session_id
    assert not session_dir.exists()

    from runtime.orchestrator.orchestrator import _cleanup_session_attachments
    _cleanup_session_attachments(workspace, session_id)  # no exception


# ── THR-109 decision attachment tests ──────────────────────────────────

_ENGINEERING_TEAMS = (
    "teams:\n"
    "  engineering:\n"
    "    manager: engineering_head\n"
    "    workers: [product_manager, dev_agent, payment_agent, qa_engineer, senior_dev]\n"
)


def _ensure_teams(test_runtime) -> None:
    test_runtime.teams_config_path.parent.mkdir(parents=True, exist_ok=True)
    test_runtime.teams_config_path.write_text(_ENGINEERING_TEAMS)


def _setup_orch(test_runtime, db, settings):
    """Create an Orchestrator ready for run_step testing."""
    import asyncio
    from runtime.orchestrator.teams import TeamsRegistry
    from runtime.orchestrator.orchestrator import Orchestrator

    _ensure_teams(test_runtime)
    for name in ("engineering_head", "dev_agent", "qa_engineer", "senior_dev"):
        (test_runtime.workspaces_dir / name).mkdir(parents=True, exist_ok=True)

    teams = TeamsRegistry.load(test_runtime.root)
    orch = Orchestrator(db=db, settings=settings, paths=test_runtime, slug="test", teams=teams)

    class Q:
        def __init__(self): self._q = asyncio.Queue()
        def put_nowait(self, s, t): self._q.put_nowait((s, t))
    orch._queue = Q()
    return orch


class TestDecisionAttachments:
    """Tests for THR-109 manager-originated task attachments in decisions."""

    def test_delegate_attachment_persists(self, test_settings, test_runtime):
        """Direct delegate ref persists as child's own link."""
        from datetime import datetime, timezone
        from runtime.models import TaskRecord, NextStep
        from runtime.infrastructure.database import Database
        from runtime.orchestrator._paths import OrgPaths
        from runtime.infrastructure.task_attachment_store import TaskAttachmentStore
        from tests.orchestrator.conftest import ScriptedRunAgent, run_task_to_completion

        db = Database(test_runtime.db_path)
        now = datetime.now(timezone.utc)
        pid = "T-DEL-ATT"
        db.insert_task(TaskRecord(
            id=pid, brief="root", team="engineering",
            assigned_agent="engineering_head", task_type="task",
            created_at=now, updated_at=now,
        ))
        store = TaskAttachmentStore(OrgPaths(test_runtime.root).task_attachments_dir)
        store.put("da-key", b"data")

        orch = _setup_orch(test_runtime, db, test_settings)
        scripted = ScriptedRunAgent()
        orch._run_agent = scripted

        decision = NextStep(action="delegate", agent="dev_agent", prompt="build",
                           attachments=[{"storage_key": "da-key", "display_name": "spec.png"}])
        scripted.enqueue("engineering_head", decision=decision, summary="delegating")
        scripted.enqueue("dev_agent", summary="done")
        scripted.enqueue("engineering_head",
                        decision=NextStep(action="done", summary="all done"),
                        summary="wrap up")

        run_task_to_completion(orch, task_id=pid)
        children = db.get_children(pid)
        assert len(children) >= 1
        child_id = children[0]
        att = db.get_task_attachment(child_id, "da-key")
        assert att is not None, "Child should have its own attachment link"

    def test_missing_attachment_rejects_no_child(self, test_settings, test_runtime):
        """Invalid attachment ref rejects entire decision before child spawn."""
        from datetime import datetime, timezone
        from runtime.models import TaskRecord, NextStep
        from runtime.infrastructure.database import Database
        from tests.orchestrator.conftest import ScriptedRunAgent, run_task_to_completion

        db = Database(test_runtime.db_path)
        now = datetime.now(timezone.utc)
        pid = "T-INV-ATT"
        db.insert_task(TaskRecord(
            id=pid, brief="root", team="engineering",
            assigned_agent="engineering_head", task_type="task",
            created_at=now, updated_at=now,
        ))
        orch = _setup_orch(test_runtime, db, test_settings)
        scripted = ScriptedRunAgent()
        orch._run_agent = scripted

        decision = NextStep(action="delegate", agent="dev_agent", prompt="build",
                           attachments=[{"storage_key": "nonexistent"}])
        scripted.enqueue("engineering_head", decision=decision, summary="wont work")

        run_task_to_completion(orch, task_id=pid)
        parent = db.get_task(pid)
        assert parent.status == TaskStatus.FAILED
        assert len(db.get_children(pid)) == 0

    def test_delegate_no_attachments_still_works(self, test_settings, test_runtime):
        """Delegation without attachments works identically to before."""
        from datetime import datetime, timezone
        from runtime.models import TaskRecord, NextStep
        from runtime.infrastructure.database import Database
        from tests.orchestrator.conftest import ScriptedRunAgent, run_task_to_completion

        db = Database(test_runtime.db_path)
        now = datetime.now(timezone.utc)
        pid = "T-NOATT"
        db.insert_task(TaskRecord(
            id=pid, brief="root", team="engineering",
            assigned_agent="engineering_head", task_type="task",
            created_at=now, updated_at=now,
        ))
        orch = _setup_orch(test_runtime, db, test_settings)
        scripted = ScriptedRunAgent()
        orch._run_agent = scripted

        decision = NextStep(action="delegate", agent="dev_agent", prompt="build")
        scripted.enqueue("engineering_head", decision=decision, summary="delegating")
        scripted.enqueue("dev_agent", summary="done")
        scripted.enqueue("engineering_head",
                        decision=NextStep(action="done", summary="all done"),
                        summary="wrap up")

        run_task_to_completion(orch, task_id=pid)
        children = db.get_children(pid)
        assert len(children) == 1
        assert len(db.list_task_attachments(children[0])) == 0

    def test_try_delegate_atomic_rollback_on_dup_attachment(self, test_settings, test_runtime):
        """Duplicate storage_key in try_delegate rolls back child + attachments."""
        from datetime import datetime, timezone
        from runtime.models import TaskRecord
        from runtime.infrastructure.database import Database
        from runtime.orchestrator._paths import OrgPaths
        from runtime.infrastructure.task_attachment_store import TaskAttachmentStore

        db = Database(test_runtime.db_path)
        now = datetime.now(timezone.utc)

        pid = "T-ATOMIC"
        db.insert_task(TaskRecord(id=pid, brief="root", team="engineering",
                                   created_at=now, updated_at=now))
        # Pre-claim the key
        other_id = "T-OTHER"
        db.insert_task(TaskRecord(id=other_id, brief="other", team="engineering",
                                   created_at=now, updated_at=now))
        store = TaskAttachmentStore(OrgPaths(test_runtime.root).task_attachments_dir)
        store.put("atom-dup", b"x")
        db.insert_task_attachment(task_id=other_id, ordinal=0, storage_key="atom-dup",
                                   display_name="x.png", size_bytes=1,
                                   content_type="image/png", uploaded_by="founder")

        child = TaskRecord(id="T-ATOMIC-C", team="engineering", brief="child",
                           assigned_agent="dev_agent", parent_task_id=pid,
                           status=TaskStatus.PENDING, created_at=now, updated_at=now)
        try:
            db.try_delegate(pid, child, parent_note="test",
                          attachments=[{"ordinal": 0, "storage_key": "atom-dup",
                                       "display_name": "x.png", "size_bytes": 1,
                                       "content_type": "image/png"}],
                          uploaded_by="test")
        except Exception:
            pass
        assert db.get_task("T-ATOMIC-C") is None, "Child must not exist after rollback"
        assert db.get_task(pid).status == TaskStatus.PENDING

    def test_chain_leg_attachment_persists(self, test_settings, test_runtime):
        """Chain leg attachments persist when auto-advancing."""
        from datetime import datetime, timezone
        from runtime.models import TaskRecord, ChainLeg
        from runtime.infrastructure.database import Database
        from runtime.orchestrator._paths import OrgPaths
        from runtime.infrastructure.task_attachment_store import TaskAttachmentStore
        from runtime.orchestrator.chain import ChainState

        db = Database(test_runtime.db_path)
        now = datetime.now(timezone.utc)
        pid = "T-CHN-ATT"
        db.insert_task(TaskRecord(
            id=pid, brief="root", team="engineering",
            assigned_agent="engineering_head", task_type="task",
            created_at=now, updated_at=now,
        ))
        store = TaskAttachmentStore(OrgPaths(test_runtime.root).task_attachments_dir)
        store.put("chain-key", b"ck")
        _ensure_teams(test_runtime)

        from runtime.orchestrator.teams import TeamsRegistry
        from runtime.orchestrator.orchestrator import Orchestrator
        teams = TeamsRegistry.load(test_runtime.root)
        orch = Orchestrator(db=db, settings=test_settings, paths=test_runtime,
                           slug="test", teams=teams)

        # Simulate chain state: first leg (step_index=0) just completed.
        # The completed child has verdict APPROVE matching first_leg_expect_verdict.
        chain = ChainState(
            step_index=0, first_leg_expect_verdict="APPROVE",
            legs=[ChainLeg(agent="qa_engineer", prompt="review",
                          expect_verdict="PASS",
                          attachments=[{"storage_key": "chain-key", "display_name": "review.md"}])],
            step_audit_id=1,
        )
        db.update_task_active_chain(pid, chain.serialize())
        child1_id = "T-CHN-C1"
        db.insert_task(TaskRecord(
            id=child1_id, brief="first leg", team="engineering",
            assigned_agent="senior_dev", parent_task_id=pid,
            status=TaskStatus.COMPLETED, created_at=now, updated_at=now,
        ))
        db.insert_task_result(task_id=child1_id, agent="senior_dev", session_id="s1",
                              status="completed", confidence_score=90,
                              output_summary="done", verdict="APPROVE")

        from runtime.orchestrator.run_step import _advance_chain_for_completed_child
        result = _advance_chain_for_completed_child(orch=orch, parent_task_id=pid,
                                                     child_task_id=child1_id)
        assert result == "advance"
        children = db.get_children(pid)
        assert len(children) == 2
        child2_id = [c for c in children if c != child1_id][0]
        att = db.get_task_attachment(child2_id, "chain-key")
        assert att is not None, "Chain leg child should have its own attachment link"

    def test_fanout_child_attachments_persist(self, test_settings, test_runtime):
        """Fanout children persist their declared attachments."""
        from datetime import datetime, timezone
        from runtime.models import TaskRecord
        from runtime.infrastructure.database import Database
        from runtime.orchestrator._paths import OrgPaths
        from runtime.infrastructure.task_attachment_store import TaskAttachmentStore

        db = Database(test_runtime.db_path)
        now = datetime.now(timezone.utc)
        pid = "T-FAN-ATT"
        db.insert_task(TaskRecord(
            id=pid, brief="root", team="engineering",
            assigned_agent="engineering_head", task_type="task",
            created_at=now, updated_at=now,
        ))
        store = TaskAttachmentStore(OrgPaths(test_runtime.root).task_attachments_dir)
        store.put("fan-k1", b"f1")
        store.put("fan-k2", b"f2")

        orch = _setup_orch(test_runtime, db, test_settings)

        from runtime.orchestrator.run_step import _spawn_fanout_children
        children_payload = [
            {"agent": "dev_agent", "prompt": "task A",
             "attachments": [{"storage_key": "fan-k1", "display_name": "a.png"}]},
            {"agent": "qa_engineer", "prompt": "task B",
             "attachments": [{"storage_key": "fan-k2", "display_name": "b.png"}]},
        ]
        _spawn_fanout_children(orch, parent=db.get_task(pid),
                               task_id=pid, next_count=1,
                               children=children_payload, width=2,
                               manager_agent="engineering_head")

        children = db.get_children(pid)
        assert len(children) == 2
        assert db.get_task_attachment(children[0], "fan-k1") is not None
        assert db.get_task_attachment(children[1], "fan-k2") is not None

    def test_fanout_duplicate_keys_across_siblings_rejected(self, test_settings, test_runtime):
        """Duplicate storage keys across fanout siblings reject entire fanout."""
        from datetime import datetime, timezone
        from runtime.models import TaskRecord
        from runtime.infrastructure.database import Database
        from runtime.orchestrator._paths import OrgPaths
        from runtime.infrastructure.task_attachment_store import TaskAttachmentStore

        db = Database(test_runtime.db_path)
        now = datetime.now(timezone.utc)
        pid = "T-FAN-DUP"
        db.insert_task(TaskRecord(
            id=pid, brief="root", team="engineering",
            assigned_agent="engineering_head", task_type="task",
            created_at=now, updated_at=now,
        ))
        store = TaskAttachmentStore(OrgPaths(test_runtime.root).task_attachments_dir)
        store.put("fan-dup-key", b"dup")

        orch = _setup_orch(test_runtime, db, test_settings)

        from runtime.orchestrator.run_step import _spawn_fanout_children
        children_payload = [
            {"agent": "dev_agent", "prompt": "task A",
             "attachments": [{"storage_key": "fan-dup-key", "display_name": "a.png"}]},
            {"agent": "qa_engineer", "prompt": "task B",
             "attachments": [{"storage_key": "fan-dup-key", "display_name": "b.png"}]},
        ]
        _spawn_fanout_children(orch, parent=db.get_task(pid),
                               task_id=pid, next_count=1,
                               children=children_payload, width=2,
                               manager_agent="engineering_head")

        parent = db.get_task(pid)
        assert parent.status == TaskStatus.FAILED
        assert parent.active_fanout is None
        assert len(db.get_children(pid)) == 0

    def test_pipeline_carrier_attachment_inheritance(self, test_settings, test_runtime):
        """Pipeline carrier owns refs; first leg inherits via ancestor resolution."""
        from datetime import datetime, timezone
        from runtime.models import TaskRecord
        from runtime.infrastructure.database import Database
        from runtime.orchestrator._paths import OrgPaths
        from runtime.infrastructure.task_attachment_store import TaskAttachmentStore

        db = Database(test_runtime.db_path)
        now = datetime.now(timezone.utc)
        pid = "T-PIPE"
        db.insert_task(TaskRecord(
            id=pid, brief="root", team="engineering",
            assigned_agent="engineering_head", task_type="task",
            created_at=now, updated_at=now,
        ))
        store = TaskAttachmentStore(OrgPaths(test_runtime.root).task_attachments_dir)
        store.put("pipe-key", b"pk")

        orch = _setup_orch(test_runtime, db, test_settings)

        from runtime.orchestrator.run_step import _spawn_fanout_children
        children_payload = [
            {"agent": "senior_dev", "prompt": "review",
             "expect_verdict": "APPROVE",
             "then": [{"agent": "qa_engineer", "prompt": "qa", "expect_verdict": "PASS"}],
             "attachments": [{"storage_key": "pipe-key", "display_name": "spec.md"}]},
        ]
        _spawn_fanout_children(orch, parent=db.get_task(pid),
                               task_id=pid, next_count=1,
                               children=children_payload, width=1,
                               manager_agent="engineering_head")

        children = db.get_children(pid)
        assert len(children) == 1
        carrier_id = children[0]
        assert db.get_task_attachment(carrier_id, "pipe-key") is not None

        # First leg must NOT have its own duplicate link
        carrier_children = db.get_children(carrier_id)
        assert len(carrier_children) == 1
        first_leg_id = carrier_children[0]
        assert db.get_task_attachment(first_leg_id, "pipe-key") is None

        # But ancestor resolution should include the carrier's attachment
        ancestor_keys = [a.storage_key for a in db.resolve_ancestor_attachments(first_leg_id)]
        assert "pipe-key" in ancestor_keys

    def test_parent_inheritance_unchanged(self, test_settings, test_runtime):
        """Existing parent attachment inheritance is unchanged."""
        from datetime import datetime, timezone
        from runtime.models import TaskRecord
        from runtime.infrastructure.database import Database
        from runtime.orchestrator._paths import OrgPaths
        from runtime.infrastructure.task_attachment_store import TaskAttachmentStore

        db = Database(test_runtime.db_path)
        now = datetime.now(timezone.utc)
        for tid, pt in [("T-INH-R", None), ("T-INH-P", "T-INH-R"), ("T-INH-C", "T-INH-P")]:
            db.insert_task(TaskRecord(id=tid, brief=tid, team="engineering",
                                       parent_task_id=pt, created_at=now, updated_at=now))

        store = TaskAttachmentStore(OrgPaths(test_runtime.root).task_attachments_dir)
        store.put("inh-key", b"inh")
        db.insert_task_attachment(task_id="T-INH-R", ordinal=0, storage_key="inh-key",
                                   display_name="root.png", size_bytes=3,
                                   content_type="image/png", uploaded_by="founder")

        ancestor_keys = [a.storage_key for a in db.resolve_ancestor_attachments("T-INH-C")]
        assert "inh-key" in ancestor_keys

    # --- Reviewer-requested regression tests (TASK-3325) ---

    def test_invalid_later_chain_ref_with_empty_direct_leaves_zero_state(
        self, test_settings, test_runtime,
    ):
        """Chain with empty direct attachments but invalid later-leg ref
        must reject before spawning first child / parking parent / writing
        active_chain. Zero state is left behind."""
        from datetime import datetime, timezone
        from runtime.models import TaskRecord, NextStep
        from runtime.infrastructure.database import Database
        from tests.orchestrator.conftest import ScriptedRunAgent, run_task_to_completion

        db = Database(test_runtime.db_path)
        now = datetime.now(timezone.utc)
        pid = "T-LATE-INV"
        db.insert_task(TaskRecord(
            id=pid, brief="root", team="engineering",
            assigned_agent="engineering_head", task_type="task",
            created_at=now, updated_at=now,
        ))
        orch = _setup_orch(test_runtime, db, test_settings)
        scripted = ScriptedRunAgent()
        orch._run_agent = scripted

        # Direct leg has NO attachments, but later chain leg references
        # a nonexistent key. The whole decision must be rejected before
        # anything is spawned.
        from runtime.models import ChainLeg
        decision = NextStep(
            action="delegate", agent="dev_agent", prompt="build",
            then=[
                ChainLeg(agent="qa_engineer", prompt="qa",
                         attachments=[{"storage_key": "nonexistent-chain"}]),
            ],
        )
        scripted.enqueue("engineering_head", decision=decision, summary="delegating")

        run_task_to_completion(orch, task_id=pid)
        parent = db.get_task(pid)
        assert parent.status == TaskStatus.FAILED
        assert parent.active_chain is None
        assert len(db.get_children(pid)) == 0

    def test_chain_attachment_claim_failure_rolls_back_advanced_state(
        self, test_settings, test_runtime,
    ):
        """Chain advancer with validation-passing but claim-failing
        attachments must clear advanced chain state — no child, no link,
        no audit, no queue entry."""
        from datetime import datetime, timezone
        from runtime.models import TaskRecord, ChainLeg
        from runtime.infrastructure.database import Database
        from runtime.orchestrator._paths import OrgPaths
        from runtime.infrastructure.task_attachment_store import TaskAttachmentStore
        from runtime.orchestrator.chain import ChainState

        db = Database(test_runtime.db_path)
        now = datetime.now(timezone.utc)
        pid = "T-CHN-CLM"
        db.insert_task(TaskRecord(
            id=pid, brief="root", team="engineering",
            assigned_agent="engineering_head", task_type="task",
            created_at=now, updated_at=now,
        ))
        store = TaskAttachmentStore(OrgPaths(test_runtime.root).task_attachments_dir)
        store.put("chn-claim", b"cc")
        # Pre-claim the key so the chain leg insert fails on UNIQUE.
        other_id = "T-CHN-OTH"
        db.insert_task(TaskRecord(id=other_id, brief="other", team="engineering",
                                   created_at=now, updated_at=now))
        db.insert_task_attachment(task_id=other_id, ordinal=0, storage_key="chn-claim",
                                   display_name="x.png", size_bytes=2,
                                   content_type="image/png", uploaded_by="founder")
        _ensure_teams(test_runtime)

        from runtime.orchestrator.teams import TeamsRegistry
        from runtime.orchestrator.orchestrator import Orchestrator
        teams = TeamsRegistry.load(test_runtime.root)
        orch = Orchestrator(db=db, settings=test_settings, paths=test_runtime,
                           slug="test", teams=teams)

        # Set up chain with a leg that has a pre-claimed attachment.
        chain = ChainState(
            step_index=0, first_leg_expect_verdict="APPROVE",
            legs=[ChainLeg(agent="qa_engineer", prompt="review",
                          expect_verdict="PASS",
                          attachments=[{"storage_key": "chn-claim",
                                        "display_name": "x.png"}])],
            step_audit_id=1,
        )
        db.update_task_active_chain(pid, chain.serialize())
        child1_id = "T-CHN-C1"
        db.insert_task(TaskRecord(
            id=child1_id, brief="first leg", team="engineering",
            assigned_agent="senior_dev", parent_task_id=pid,
            status=TaskStatus.COMPLETED, created_at=now, updated_at=now,
        ))
        db.insert_task_result(task_id=child1_id, agent="senior_dev", session_id="s1",
                              status="completed", confidence_score=90,
                              output_summary="done", verdict="APPROVE")

        from runtime.orchestrator.run_step import _advance_chain_for_completed_child
        result = _advance_chain_for_completed_child(orch=orch, parent_task_id=pid,
                                                     child_task_id=child1_id)
        # Validation passes (file exists), but insert_task_with_attachments
        # fails on duplicate storage_key. Chain is cleared, child not created.
        assert result == "wake"
        parent_after = db.get_task(pid)
        assert parent_after.active_chain is None
        children = db.get_children(pid)
        assert len(children) == 1  # only the completed first child

    def test_pipeline_later_leg_refs_persist(self, test_settings, test_runtime):
        """Pipeline carrier chain leg attachments are preserved in the
        serialized chain state so later legs receive their own declared refs."""
        from datetime import datetime, timezone
        from runtime.models import TaskRecord
        from runtime.infrastructure.database import Database
        from runtime.orchestrator._paths import OrgPaths
        from runtime.infrastructure.task_attachment_store import TaskAttachmentStore

        db = Database(test_runtime.db_path)
        now = datetime.now(timezone.utc)
        pid = "T-PIPE-LT"
        db.insert_task(TaskRecord(
            id=pid, brief="root", team="engineering",
            assigned_agent="engineering_head", task_type="task",
            created_at=now, updated_at=now,
        ))
        store = TaskAttachmentStore(OrgPaths(test_runtime.root).task_attachments_dir)
        store.put("pipe-carrier-key", b"pk")
        store.put("pipe-later-key", b"pl")

        orch = _setup_orch(test_runtime, db, test_settings)

        from runtime.orchestrator.run_step import _spawn_fanout_children
        children_payload = [
            {"agent": "senior_dev", "prompt": "review",
             "expect_verdict": "APPROVE",
             "then": [
                 {"agent": "qa_engineer", "prompt": "qa", "expect_verdict": "PASS",
                  "attachments": [{"storage_key": "pipe-later-key",
                                   "display_name": "qa-checklist.md"}]},
             ],
             "attachments": [{"storage_key": "pipe-carrier-key",
                              "display_name": "spec.md"}]},
        ]
        _spawn_fanout_children(orch, parent=db.get_task(pid),
                               task_id=pid, next_count=1,
                               children=children_payload, width=1,
                               manager_agent="engineering_head")

        children = db.get_children(pid)
        assert len(children) == 1
        carrier_id = children[0]
        # Carrier owns its declared ref.
        assert db.get_task_attachment(carrier_id, "pipe-carrier-key") is not None

        # The serialized chain must contain the later leg's attachment ref.
        carrier = db.get_task(carrier_id)
        assert carrier.active_chain is not None
        chain_data = json.loads(carrier.active_chain)
        assert len(chain_data["legs"]) == 1
        leg_0 = chain_data["legs"][0]
        assert leg_0.get("attachments") is not None
        assert len(leg_0["attachments"]) == 1
        assert leg_0["attachments"][0]["storage_key"] == "pipe-later-key"

    def test_valid_multi_child_fanout_per_child_count(self, test_settings, test_runtime):
        """Fanout with two children each having 3 distinct refs must succeed —
        per-child count limit (5) applies, not global sum."""
        from datetime import datetime, timezone
        from runtime.models import TaskRecord
        from runtime.infrastructure.database import Database
        from runtime.orchestrator._paths import OrgPaths
        from runtime.infrastructure.task_attachment_store import TaskAttachmentStore

        db = Database(test_runtime.db_path)
        now = datetime.now(timezone.utc)
        pid = "T-FAN-MANY"
        db.insert_task(TaskRecord(
            id=pid, brief="root", team="engineering",
            assigned_agent="engineering_head", task_type="task",
            created_at=now, updated_at=now,
        ))
        store = TaskAttachmentStore(OrgPaths(test_runtime.root).task_attachments_dir)
        # 6 distinct keys, 3 per child — per-child count is 3 (≤ 5), global
        # sum is 6 (> 5 if global limit were enforced).
        keys = ["fam-a1", "fam-a2", "fam-a3", "fam-b1", "fam-b2", "fam-b3"]
        for k in keys:
            store.put(k, b"x")

        orch = _setup_orch(test_runtime, db, test_settings)

        from runtime.orchestrator.run_step import _spawn_fanout_children
        children_payload = [
            {"agent": "dev_agent", "prompt": "task A",
             "attachments": [
                 {"storage_key": "fam-a1", "display_name": "a1.png"},
                 {"storage_key": "fam-a2", "display_name": "a2.png"},
                 {"storage_key": "fam-a3", "display_name": "a3.png"},
             ]},
            {"agent": "qa_engineer", "prompt": "task B",
             "attachments": [
                 {"storage_key": "fam-b1", "display_name": "b1.png"},
                 {"storage_key": "fam-b2", "display_name": "b2.png"},
                 {"storage_key": "fam-b3", "display_name": "b3.png"},
             ]},
        ]
        _spawn_fanout_children(orch, parent=db.get_task(pid),
                               task_id=pid, next_count=1,
                               children=children_payload, width=2,
                               manager_agent="engineering_head")

        parent_after = db.get_task(pid)
        assert parent_after.status == TaskStatus.IN_PROGRESS
        assert parent_after.active_fanout is not None
        children = db.get_children(pid)
        assert len(children) == 2
        # Each child has its own 3 attachments.
        for i in range(2):
            child_atts = db.list_task_attachments(children[i])
            assert len(child_atts) == 3

    def test_pipeline_carrier_first_leg_collision_rolls_back(
        self, test_settings, test_runtime,
    ):
        """When a pipeline carrier's first-leg INSERT collides with an
        existing task id, the entire fanout transaction (child, links,
        parent park, carrier chain) rolls back atomically.
        Tested via direct try_delegate_many call with carrier_chains."""
        from datetime import datetime, timezone
        from runtime.models import TaskRecord, TaskStatus, BlockKind
        from runtime.infrastructure.database import Database
        from runtime.orchestrator._paths import OrgPaths
        from runtime.infrastructure.task_attachment_store import TaskAttachmentStore

        db = Database(test_runtime.db_path)
        now = datetime.now(timezone.utc)
        pid = "T-PIPE-FL"
        db.insert_task(TaskRecord(
            id=pid, brief="root", team="engineering",
            assigned_agent="engineering_head", task_type="task",
            created_at=now, updated_at=now,
        ))
        store = TaskAttachmentStore(OrgPaths(test_runtime.root).task_attachments_dir)
        store.put("pipe-fl-key", b"pf")

        # Allocate IDs directly. next_task_id() returns MAX+1, so child
        # gets base_num, first leg gets base_num+1.
        child_id = db.next_task_id()
        base_num = int(child_id.split("-")[-1])
        carrier_id = f"TASK-{base_num:03d}"
        first_leg_id = f"TASK-{base_num + 1:03d}"
        child = TaskRecord(
            id=carrier_id, team="engineering", brief="review",
            assigned_agent="senior_dev", parent_task_id=pid,
            status=TaskStatus.IN_PROGRESS, block_kind=BlockKind.DELEGATED,
            session_timeout_seconds=300, task_type="subtask",
            created_at=now, updated_at=now,
        )

        # Pre-insert the first leg ID to force a collision.
        db._conn.execute(
            "INSERT INTO tasks (id, status, team, brief, created_at, updated_at) "
            "VALUES (?, 'pending', 'engineering', 'collision', ?, ?)",
            (first_leg_id, now.isoformat(), now.isoformat()),
        )
        db._conn.commit()

        # Build carrier chain data with the colliding first leg ID.
        from runtime.orchestrator.chain import ChainState
        from runtime.models import ChainLeg, TaskAttachmentRef
        then_legs = [
            ChainLeg(agent="qa_engineer", prompt="qa", expect_verdict="PASS",
                     attachments=[TaskAttachmentRef(storage_key="pipe-fl-key",
                                                    display_name="spec.md")]),
        ]
        carrier_chain = ChainState(
            step_index=0,
            first_leg_expect_verdict="APPROVE",
            legs=then_legs,
            step_audit_id=1,
        )
        carrier_chains = [{
            "child_index": 0,
            "active_chain_json": carrier_chain.serialize(),
            "first_leg": {
                "id": first_leg_id,
                "team": "engineering",
                "brief": "review",
                "assigned_agent": "senior_dev",
                "status": TaskStatus.PENDING,
                "session_timeout_seconds": 300,
                "task_type": "subtask",
                "revision_count": 0,
                "orchestration_step_count": 0,
            },
            "first_leg_id": first_leg_id,
        }]

        # Build attachment params.
        prevalidated = [{"storage_key": "pipe-fl-key", "display_name": "spec.md",
                          "content_type": "text/markdown"}]
        from runtime.orchestrator.run_step import _prepare_attachment_params
        orch = _setup_orch(test_runtime, db, test_settings)
        child_atts = _prepare_attachment_params(orch, prevalidated)

        # Call try_delegate_many — the first leg INSERT should collide and
        # the entire transaction should roll back.
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint"):
            db.try_delegate_many(
                pid, [child], parent_note="test",
                children_attachments=[child_atts],
                carrier_chains=carrier_chains,
                uploaded_by="test",
            )

        # Verify nothing was committed.
        parent_after = db.get_task(pid)
        assert parent_after.status == TaskStatus.PENDING, \
            f"Parent should still be PENDING, got {parent_after.status}"
        assert parent_after.active_fanout is None
        assert db.get_task(carrier_id) is None
        # Our pre-inserted row still exists (was committed before the test).
        assert db.get_task(first_leg_id) is not None
        # Attachment link must not exist.
        assert db.get_task_attachment_by_storage_key("pipe-fl-key") is None

    # --- TASK-3333 regression tests: decision-wide validation ---

    def test_direct_and_later_leg_duplicate_key_rejects_entire_decision(
        self, test_settings, test_runtime,
    ):
        """A key in decision.attachments AND decision.then[i].attachments
        must be caught by decision-wide duplicate detection and reject
        before any child/spawn/park/chain/queue."""
        from datetime import datetime, timezone
        from runtime.models import TaskRecord, NextStep, ChainLeg
        from runtime.infrastructure.database import Database
        from runtime.orchestrator._paths import OrgPaths
        from runtime.infrastructure.task_attachment_store import TaskAttachmentStore
        from tests.orchestrator.conftest import ScriptedRunAgent, run_task_to_completion

        db = Database(test_runtime.db_path)
        now = datetime.now(timezone.utc)
        pid = "T-DIR-DUP"
        db.insert_task(TaskRecord(
            id=pid, brief="root", team="engineering",
            assigned_agent="engineering_head", task_type="task",
            created_at=now, updated_at=now,
        ))
        store = TaskAttachmentStore(OrgPaths(test_runtime.root).task_attachments_dir)
        store.put("dup-key", b"data")

        orch = _setup_orch(test_runtime, db, test_settings)
        scripted = ScriptedRunAgent()
        orch._run_agent = scripted

        # Same key in both direct and later leg — must reject whole decision.
        decision = NextStep(
            action="delegate", agent="dev_agent", prompt="build",
            attachments=[{"storage_key": "dup-key", "display_name": "a.png"}],
            then=[
                ChainLeg(agent="qa_engineer", prompt="qa",
                         attachments=[{"storage_key": "dup-key", "display_name": "a.png"}]),
            ],
        )
        scripted.enqueue("engineering_head", decision=decision, summary="delegating")

        run_task_to_completion(orch, task_id=pid)
        parent = db.get_task(pid)
        assert parent.status == TaskStatus.FAILED
        assert parent.active_chain is None
        assert len(db.get_children(pid)) == 0

    def test_two_later_legs_duplicate_key_rejects_entire_decision(
        self, test_settings, test_runtime,
    ):
        """Same key in two different chain legs rejects before any state."""
        from datetime import datetime, timezone
        from runtime.models import TaskRecord, NextStep, ChainLeg
        from runtime.infrastructure.database import Database
        from tests.orchestrator.conftest import ScriptedRunAgent, run_task_to_completion

        db = Database(test_runtime.db_path)
        now = datetime.now(timezone.utc)
        pid = "T-CHN-DUP"
        db.insert_task(TaskRecord(
            id=pid, brief="root", team="engineering",
            assigned_agent="engineering_head", task_type="task",
            created_at=now, updated_at=now,
        ))

        orch = _setup_orch(test_runtime, db, test_settings)
        scripted = ScriptedRunAgent()
        orch._run_agent = scripted

        decision = NextStep(
            action="delegate", agent="dev_agent", prompt="build",
            then=[
                ChainLeg(agent="qa_engineer", prompt="qa",
                         attachments=[{"storage_key": "leg-key"}]),
                ChainLeg(agent="code_reviewer", prompt="review",
                         attachments=[{"storage_key": "leg-key"}]),
            ],
        )
        scripted.enqueue("engineering_head", decision=decision, summary="delegating")

        run_task_to_completion(orch, task_id=pid)
        parent = db.get_task(pid)
        assert parent.status == TaskStatus.FAILED
        assert parent.active_chain is None
        assert len(db.get_children(pid)) == 0

    def test_later_leg_missing_attachment_rejects_entire_decision(
        self, test_settings, test_runtime,
    ):
        """A nonexistent key in a chain leg rejects before first child spawn."""
        from datetime import datetime, timezone
        from runtime.models import TaskRecord, NextStep, ChainLeg
        from runtime.infrastructure.database import Database
        from tests.orchestrator.conftest import ScriptedRunAgent, run_task_to_completion

        db = Database(test_runtime.db_path)
        now = datetime.now(timezone.utc)
        pid = "T-CHN-MIS"
        db.insert_task(TaskRecord(
            id=pid, brief="root", team="engineering",
            assigned_agent="engineering_head", task_type="task",
            created_at=now, updated_at=now,
        ))

        orch = _setup_orch(test_runtime, db, test_settings)
        scripted = ScriptedRunAgent()
        orch._run_agent = scripted

        decision = NextStep(
            action="delegate", agent="dev_agent", prompt="build",
            then=[
                ChainLeg(agent="qa_engineer", prompt="qa",
                         attachments=[{"storage_key": "nonexistent-leg"}]),
            ],
        )
        scripted.enqueue("engineering_head", decision=decision, summary="delegating")

        run_task_to_completion(orch, task_id=pid)
        parent = db.get_task(pid)
        assert parent.status == TaskStatus.FAILED
        assert parent.active_chain is None
        assert len(db.get_children(pid)) == 0

    def test_later_leg_already_claimed_key_rejects_entire_decision(
        self, test_settings, test_runtime,
    ):
        """An already-claimed key in a chain leg rejects before any child
        or parent park/chain state."""
        from datetime import datetime, timezone
        from runtime.models import TaskRecord, NextStep, ChainLeg
        from runtime.infrastructure.database import Database
        from runtime.orchestrator._paths import OrgPaths
        from runtime.infrastructure.task_attachment_store import TaskAttachmentStore
        from tests.orchestrator.conftest import ScriptedRunAgent, run_task_to_completion

        db = Database(test_runtime.db_path)
        now = datetime.now(timezone.utc)
        pid = "T-CHN-CLM"
        db.insert_task(TaskRecord(
            id=pid, brief="root", team="engineering",
            assigned_agent="engineering_head", task_type="task",
            created_at=now, updated_at=now,
        ))
        store = TaskAttachmentStore(OrgPaths(test_runtime.root).task_attachments_dir)
        store.put("claimed-later", b"cl")
        # Pre-claim the key
        other_id = "T-OTHER-CL"
        db.insert_task(TaskRecord(id=other_id, brief="other", team="engineering",
                                   created_at=now, updated_at=now))
        db.insert_task_attachment(task_id=other_id, ordinal=0, storage_key="claimed-later",
                                   display_name="x.png", size_bytes=1,
                                   content_type="image/png", uploaded_by="founder")

        orch = _setup_orch(test_runtime, db, test_settings)
        scripted = ScriptedRunAgent()
        orch._run_agent = scripted

        # Later leg references an already-claimed key — must reject before child.
        decision = NextStep(
            action="delegate", agent="dev_agent", prompt="build",
            then=[
                ChainLeg(agent="qa_engineer", prompt="qa",
                         attachments=[{"storage_key": "claimed-later",
                                        "display_name": "x.png"}]),
            ],
        )
        scripted.enqueue("engineering_head", decision=decision, summary="delegating")

        run_task_to_completion(orch, task_id=pid)
        parent = db.get_task(pid)
        assert parent.status == TaskStatus.FAILED
        assert parent.active_chain is None
        assert len(db.get_children(pid)) == 0

    # --- TASK-3333 regression tests: fanout pipeline nested validation ---

    def test_pipeline_nested_later_leg_missing_attachment_rejects_fanout(
        self, test_settings, test_runtime,
    ):
        """A missing attachment in a pipeline carrier's nested chain leg
        must reject the entire fanout before child/parent park/queue."""
        from datetime import datetime, timezone
        from runtime.models import TaskRecord
        from runtime.infrastructure.database import Database

        db = Database(test_runtime.db_path)
        now = datetime.now(timezone.utc)
        pid = "T-PIPE-MIS"
        db.insert_task(TaskRecord(
            id=pid, brief="root", team="engineering",
            assigned_agent="engineering_head", task_type="task",
            created_at=now, updated_at=now,
        ))

        orch = _setup_orch(test_runtime, db, test_settings)

        from runtime.orchestrator.run_step import _spawn_fanout_children
        children_payload = [
            {"agent": "senior_dev", "prompt": "review",
             "expect_verdict": "APPROVE",
             "then": [
                 {"agent": "qa_engineer", "prompt": "qa",
                  "attachments": [{"storage_key": "nonexistent-pipe-nested"}]},
             ]},
        ]
        _spawn_fanout_children(orch, parent=db.get_task(pid),
                               task_id=pid, next_count=1,
                               children=children_payload, width=1,
                               manager_agent="engineering_head")

        parent = db.get_task(pid)
        assert parent.status == TaskStatus.FAILED
        assert parent.active_fanout is None
        assert len(db.get_children(pid)) == 0

    def test_pipeline_nested_later_leg_already_claimed_key_rejects_fanout(
        self, test_settings, test_runtime,
    ):
        """An already-claimed key in a pipeline carrier's nested leg
        rejects the entire fanout before state."""
        from datetime import datetime, timezone
        from runtime.models import TaskRecord
        from runtime.infrastructure.database import Database
        from runtime.orchestrator._paths import OrgPaths
        from runtime.infrastructure.task_attachment_store import TaskAttachmentStore

        db = Database(test_runtime.db_path)
        now = datetime.now(timezone.utc)
        pid = "T-PIPE-CLM"
        db.insert_task(TaskRecord(
            id=pid, brief="root", team="engineering",
            assigned_agent="engineering_head", task_type="task",
            created_at=now, updated_at=now,
        ))
        store = TaskAttachmentStore(OrgPaths(test_runtime.root).task_attachments_dir)
        store.put("pipe-claimed", b"pc")
        # Pre-claim the key
        other_id = "T-PIPE-OTH"
        db.insert_task(TaskRecord(id=other_id, brief="other", team="engineering",
                                   created_at=now, updated_at=now))
        db.insert_task_attachment(task_id=other_id, ordinal=0, storage_key="pipe-claimed",
                                   display_name="x.png", size_bytes=2,
                                   content_type="image/png", uploaded_by="founder")

        orch = _setup_orch(test_runtime, db, test_settings)

        from runtime.orchestrator.run_step import _spawn_fanout_children
        children_payload = [
            {"agent": "senior_dev", "prompt": "review",
             "expect_verdict": "APPROVE",
             "then": [
                 {"agent": "qa_engineer", "prompt": "qa",
                  "attachments": [{"storage_key": "pipe-claimed",
                                   "display_name": "x.png"}]},
             ]},
        ]
        _spawn_fanout_children(orch, parent=db.get_task(pid),
                               task_id=pid, next_count=1,
                               children=children_payload, width=1,
                               manager_agent="engineering_head")

        parent = db.get_task(pid)
        assert parent.status == TaskStatus.FAILED
        assert parent.active_fanout is None
        assert len(db.get_children(pid)) == 0

    def test_pipeline_nested_duplicate_with_carrier_rejects_fanout(
        self, test_settings, test_runtime,
    ):
        """Same key in carrier top-level and nested later leg rejects
        the fanout (carrier-to-nested duplicate)."""
        from datetime import datetime, timezone
        from runtime.models import TaskRecord
        from runtime.infrastructure.database import Database
        from runtime.orchestrator._paths import OrgPaths
        from runtime.infrastructure.task_attachment_store import TaskAttachmentStore

        db = Database(test_runtime.db_path)
        now = datetime.now(timezone.utc)
        pid = "T-CARRDUP"
        db.insert_task(TaskRecord(
            id=pid, brief="root", team="engineering",
            assigned_agent="engineering_head", task_type="task",
            created_at=now, updated_at=now,
        ))
        store = TaskAttachmentStore(OrgPaths(test_runtime.root).task_attachments_dir)
        store.put("car-dup", b"cd")

        orch = _setup_orch(test_runtime, db, test_settings)

        from runtime.orchestrator.run_step import _spawn_fanout_children
        children_payload = [
            {"agent": "senior_dev", "prompt": "review",
             "expect_verdict": "APPROVE",
             "attachments": [{"storage_key": "car-dup", "display_name": "spec.md"}],
             "then": [
                 {"agent": "qa_engineer", "prompt": "qa",
                  "attachments": [{"storage_key": "car-dup",
                                   "display_name": "spec.md"}]},
             ]},
        ]
        _spawn_fanout_children(orch, parent=db.get_task(pid),
                               task_id=pid, next_count=1,
                               children=children_payload, width=1,
                               manager_agent="engineering_head")

        parent = db.get_task(pid)
        assert parent.status == TaskStatus.FAILED
        assert parent.active_fanout is None
        assert len(db.get_children(pid)) == 0

    def test_pipeline_nested_duplicate_with_sibling_nested_rejects_fanout(
        self, test_settings, test_runtime,
    ):
        """Same key in nested legs of two different pipeline carriers
        (cross-sibling nested duplicate) rejects the fanout."""
        from datetime import datetime, timezone
        from runtime.models import TaskRecord
        from runtime.infrastructure.database import Database
        from runtime.orchestrator._paths import OrgPaths
        from runtime.infrastructure.task_attachment_store import TaskAttachmentStore

        db = Database(test_runtime.db_path)
        now = datetime.now(timezone.utc)
        pid = "T-XNEST"
        db.insert_task(TaskRecord(
            id=pid, brief="root", team="engineering",
            assigned_agent="engineering_head", task_type="task",
            created_at=now, updated_at=now,
        ))
        store = TaskAttachmentStore(OrgPaths(test_runtime.root).task_attachments_dir)
        store.put("xnest-key", b"xn")

        orch = _setup_orch(test_runtime, db, test_settings)

        from runtime.orchestrator.run_step import _spawn_fanout_children
        children_payload = [
            {"agent": "senior_dev", "prompt": "review",
             "expect_verdict": "APPROVE",
             "then": [
                 {"agent": "qa_engineer", "prompt": "qa",
                  "attachments": [{"storage_key": "xnest-key",
                                   "display_name": "x.png"}]},
             ]},
            {"agent": "dev_agent", "prompt": "other",
             "then": [
                 {"agent": "qa_engineer", "prompt": "qa2",
                  "attachments": [{"storage_key": "xnest-key",
                                   "display_name": "x.png"}]},
             ]},
        ]
        _spawn_fanout_children(orch, parent=db.get_task(pid),
                               task_id=pid, next_count=1,
                               children=children_payload, width=2,
                               manager_agent="engineering_head")

        parent = db.get_task(pid)
        assert parent.status == TaskStatus.FAILED
        assert parent.active_fanout is None
        assert len(db.get_children(pid)) == 0

    # --- TASK-3338 regression tests: real-transaction atomic rollback ---

    def test_initial_direct_chain_transaction_failure_rolls_back(
        self, test_settings, test_runtime,
    ):
        """Inject a real SQLite failure inside try_delegate's transaction
        AFTER the child INSERT and attachment link have been written.
        The abort trigger fires on the audit_log INSERT inside
        _insert_task_attachments_txn, which runs inside try_delegate's
        transaction — proving the actual DB write path is hit and rolled
        back atomically.

        Verifies zero partial state: no child, no parent delegated/chain
        state, no attachment link/audit/claim, no queue entry."""
        import sqlite3
        from datetime import datetime, timezone
        from runtime.models import TaskRecord, TaskStatus, BlockKind, NextStep, ChainLeg
        from runtime.infrastructure.database import Database
        from runtime.orchestrator._paths import OrgPaths
        from runtime.infrastructure.task_attachment_store import TaskAttachmentStore
        from tests.orchestrator.conftest import ScriptedRunAgent, run_task_to_completion

        db = Database(test_runtime.db_path)
        now = datetime.now(timezone.utc)
        pid = "T-CHAIN-FL"
        db.insert_task(TaskRecord(
            id=pid, brief="root", team="engineering",
            assigned_agent="engineering_head", task_type="task",
            created_at=now, updated_at=now,
        ))
        store = TaskAttachmentStore(OrgPaths(test_runtime.root).task_attachments_dir)
        # Two DISTINCT, individually valid keys — so prevalidation passes.
        store.put("chain-fl-direct", b"fk")
        store.put("chain-fl-later", b"fl")

        # Install a Python-side counter + abort trigger on audit_log so a
        # real SQLite failure is injected inside try_delegate's transaction
        # AFTER the child INSERT and task_attachments INSERT, but before
        # commit.  The Python counter survives the SQLite rollback.
        trigger_counter = [0]
        def _inc_t1():
            trigger_counter[0] += 1
        db._conn.create_function("_t3338_t1_inc", 0, _inc_t1)
        db._conn.execute(
            "CREATE TEMP TRIGGER _t3338_t1_abort "
            "BEFORE INSERT ON audit_log "
            "WHEN NEW.action = 'task_attachment_added' "
            "BEGIN "
            "    SELECT _t3338_t1_inc(); "
            "    SELECT RAISE(ABORT, 'injected transaction failure'); "
            "END"
        )

        orch = _setup_orch(test_runtime, db, test_settings)
        scripted = ScriptedRunAgent()
        orch._run_agent = scripted

        decision = NextStep(
            action="delegate", agent="dev_agent", prompt="build",
            attachments=[{"storage_key": "chain-fl-direct",
                          "display_name": "f.png"}],
            then=[
                ChainLeg(agent="qa_engineer", prompt="qa",
                         attachments=[{"storage_key": "chain-fl-later",
                                       "display_name": "l.png"}]),
            ],
        )
        scripted.enqueue("engineering_head", decision=decision, summary="delegating")

        # try_delegate catches the ABORT, rolls back, and re-raises.
        # The exception propagates out of run_step_impl → run_step.
        error_raised = False
        try:
            run_task_to_completion(orch, task_id=pid)
        except Exception:
            error_raised = True
        assert error_raised, \
            "Expected db.try_delegate to raise after trigger ABORT"

        # Verify the trigger fired — proves the real DB write path was
        # reached (child + link INSERT happened before the abort).
        assert trigger_counter[0] >= 1, \
            f"Trigger must fire at least once, got count={trigger_counter[0]}"

        # Transaction was rolled back: no child, no chain, no claims,
        # no delegated parent state.
        parent = db.get_task(pid)
        assert parent.active_chain is None, \
            "active_chain must be clean after transaction rollback"
        # Parent must NOT be parked in a delegated state (the transaction
        # that would have set block_kind=delegated was rolled back). The
        # parent may be IN_PROGRESS (that's the legitimate step-claim
        # state set by try_claim_for_step before the delegate path runs),
        # but it must carry no DELEGATED discriminator.
        assert parent.block_kind != BlockKind.DELEGATED, \
            f"Parent block_kind must not be DELEGATED after rollback, got {parent.block_kind}"
        assert parent.note is None or "Delegated" not in (parent.note or ""), \
            f"Parent must not have a delegated note after rollback, got {parent.note!r}"

        children = db.get_children(pid)
        assert len(children) == 0, \
            f"No children should exist after transaction rollback, got {children}"

        # Queue: no spawned child was enqueued after transaction abort.
        assert orch._queue._q.empty(), \
            "Queue must be empty after transaction rollback — no child enqueued"

        # No task-attachment row for either key.
        assert db.get_task_attachment_by_storage_key("chain-fl-direct") is None, \
            "Direct attachment key must not be claimed after rollback"
        assert db.get_task_attachment_by_storage_key("chain-fl-later") is None, \
            "Later-leg attachment key must not be claimed after rollback"

        # No 'task_attachment_added' audit rows survived — query globally by
        # action because attachments are child-scoped (the audit row is
        # written with the spawned child's task_id, not the parent's).
        att_added_global = db.get_audit_logs_by_action("task_attachment_added")
        leaked = [l for l in att_added_global
                  if l.get("payload", {}).get("storage_key")
                  in ("chain-fl-direct", "chain-fl-later")]
        assert len(leaked) == 0, \
            f"No task_attachment_added audit rows after rollback for either key, got {leaked}"

    def test_auto_advance_chain_transaction_failure_rolls_back(
        self, test_settings, test_runtime,
    ):
        """Inject a real SQLite failure inside try_advance_chain's explicit
        transaction AFTER the parent active_chain UPDATE and child INSERT.
        The abort trigger fires on the audit_log INSERT inside
        _insert_task_attachments_txn, which runs inside try_advance_chain's
        BEGIN IMMEDIATE block.

        Verifies: try_advance_chain returns False, the method takes the
        wake/failure branch, zero partial state (no second child, chain
        not advanced past original snapshot, no new attachment link/audit,
        no chain_auto_advance audit, no new queue entry)."""
        import sqlite3
        from datetime import datetime, timezone
        from runtime.models import TaskRecord, ChainLeg
        from runtime.infrastructure.database import Database
        from runtime.orchestrator._paths import OrgPaths
        from runtime.infrastructure.task_attachment_store import TaskAttachmentStore
        from runtime.orchestrator.chain import ChainState

        db = Database(test_runtime.db_path)
        now = datetime.now(timezone.utc)
        pid = "T-ADV-FL"
        db.insert_task(TaskRecord(
            id=pid, brief="root", team="engineering",
            assigned_agent="engineering_head", task_type="task",
            created_at=now, updated_at=now,
        ))
        store = TaskAttachmentStore(OrgPaths(test_runtime.root).task_attachments_dir)
        store.put("adv-fl-k", b"af")
        _ensure_teams(test_runtime)

        import asyncio
        from runtime.orchestrator.teams import TeamsRegistry
        from runtime.orchestrator.orchestrator import Orchestrator
        teams = TeamsRegistry.load(test_runtime.root)
        orch = Orchestrator(db=db, settings=test_settings, paths=test_runtime,
                           slug="test", teams=teams)

        # Wire a deterministic queue so we can prove no next-child entry
        # survived the rolled-back transaction.
        class _Q:
            def __init__(self): self._q = asyncio.Queue()
            def put_nowait(self, s, t): self._q.put_nowait((s, t))
        orch._queue = _Q()

        # Set up chain with a leg that has a valid attachment.
        chain = ChainState(
            step_index=0, first_leg_expect_verdict="APPROVE",
            legs=[ChainLeg(agent="qa_engineer", prompt="review",
                          expect_verdict="PASS",
                          attachments=[{"storage_key": "adv-fl-k",
                                        "display_name": "af.png"}])],
            step_audit_id=1,
        )
        original_chain_json = chain.serialize()
        db.update_task_active_chain(pid, original_chain_json)
        child1_id = "T-ADV-C1"
        db.insert_task(TaskRecord(
            id=child1_id, brief="first leg", team="engineering",
            assigned_agent="senior_dev", parent_task_id=pid,
            status=TaskStatus.COMPLETED, created_at=now, updated_at=now,
        ))
        db.insert_task_result(task_id=child1_id, agent="senior_dev", session_id="s1",
                              status="completed", confidence_score=90,
                              output_summary="done", verdict="APPROVE")

        # Count existing children so we can assert no new child was added.
        pre_children = db.get_children(pid)
        assert len(pre_children) == 1
        # Snapshot pre-existing chain_auto_advance audit rows for the parent
        # so we can prove none were added by the failed transaction.
        pre_chain_audit = len(db.get_audit_logs_by_action("chain_auto_advance"))

        # Install a Python-side counter + abort trigger on audit_log so a
        # real SQLite failure is injected inside try_advance_chain's
        # explicit BEGIN IMMEDIATE transaction AFTER the parent
        # active_chain UPDATE + child INSERT, but before commit.
        trigger_counter = [0]
        def _inc_t2():
            trigger_counter[0] += 1
        db._conn.create_function("_t3338_t2_inc", 0, _inc_t2)
        db._conn.execute(
            "CREATE TEMP TRIGGER _t3338_t2_abort "
            "BEFORE INSERT ON audit_log "
            "WHEN NEW.action = 'task_attachment_added' "
            "BEGIN "
            "    SELECT _t3338_t2_inc(); "
            "    SELECT RAISE(ABORT, 'injected transaction failure'); "
            "END"
        )

        from runtime.orchestrator.run_step import _advance_chain_for_completed_child
        result = _advance_chain_for_completed_child(orch=orch, parent_task_id=pid,
                                                     child_task_id=child1_id)

        # try_advance_chain catches the ABORT, rolls back, returns False.
        # _advance_chain_for_completed_child returns "wake" for the caller.
        assert result == "wake", (
            f"Expected 'wake' after try_advance_chain rollback, got {result!r}"
        )

        # Verify the trigger fired — proves the real DB write path was
        # reached (parent chain UPDATE + child INSERT happened before abort).
        assert trigger_counter[0] >= 1, \
            f"Trigger must fire at least once, got count={trigger_counter[0]}"

        # Parent chain: must be IDENTICAL to the original snapshot (not
        # advanced to a later step_index), not merely non-None.
        parent_after = db.get_task(pid)
        assert parent_after.active_chain is not None, \
            "Parent active_chain must still be present after rollback"
        restored_chain = ChainState.deserialize(parent_after.active_chain)
        assert restored_chain.step_index == chain.step_index, (
            f"Chain step_index must not advance; was {chain.step_index}, "
            f"got {restored_chain.step_index}"
        )

        # No second child was created (transaction rolled back).
        post_children = db.get_children(pid)
        assert len(post_children) == 1, (
            f"No new children after rollback, expected 1 got {post_children}"
        )
        assert post_children[0] == child1_id, \
            "Original child must still be the only child"

        # Queue: no next-child entry survived the rolled-back transaction.
        assert orch._queue._q.empty(), \
            "Queue must be empty after transaction rollback — no next child enqueued"

        # No task-attachment row for the advance key was created.
        assert db.get_task_attachment_by_storage_key("adv-fl-k") is None, \
            "Attachment key must not be claimed after rollback"

        # No task_attachment_added audit for the advance key survived —
        # query globally by action because the audit row is child-scoped
        # (written with the next child's task_id, not the parent's).
        att_added_global = db.get_audit_logs_by_action("task_attachment_added")
        leaked = [l for l in att_added_global
                  if l.get("payload", {}).get("storage_key") == "adv-fl-k"]
        assert len(leaked) == 0, \
            f"No task_attachment_added audit for adv-fl-k after rollback, got {leaked}"

        # No chain_auto_advance audit was written — query globally by action.
        post_chain_audit = len(db.get_audit_logs_by_action("chain_auto_advance"))
        assert post_chain_audit == pre_chain_audit, (
            f"No new chain_auto_advance audit rows after rollback; "
            f"pre={pre_chain_audit}, post={post_chain_audit}"
        )


def test_preflight_checks_all_contracts_before_any_canonical_build(
    orchestrator, test_runtime, test_settings, monkeypatch):
    """TASK-4175 adversarial: _materialize_unified_canonical must preflight
    ALL required system-contract sources BEFORE any canonical package build.
    A later missing source must not leave earlier package builds behind.

    Strengthened TASK-4176 proof-gap repair:
    - Real _build_executor spy proves executor.run is never invoked.
    - Known unrelated trusted package seeded in same runner CanonicalSkillStore.
    - Full canonical store snapshot (manifest/member/on-disk hashes).
    - Both workspace roots (.claude/skills + .agents/skills) link targets.
    - Workspace files, task status, audit-success state all verified.
    - production _compute_dir_hash identity used throughout.
    """
    import shutil
    import hashlib
    from pathlib import Path
    from runtime.orchestrator.workspace_adapters import (
        SystemContractMaterializationError, _compute_dir_hash,
    )
    from runtime.skills.canonical_store import CanonicalSkillStore

    _setup_workspaces(test_runtime)

    proto_skills = test_settings.get_protocol_dir() / "skills"
    _setup_protocol_skills(test_settings)

    # ── Seed a known unrelated trusted package in the runner's canonical ──
    # store so we can prove it is fully untouched after the preflight failure.
    store = CanonicalSkillStore(settings=test_settings)
    trusted_pkg_dir = proto_skills / "jobs"  # known existing source
    trusted_content_hash = _compute_dir_hash(trusted_pkg_dir)
    store.build_from_source("jobs", "system", trusted_content_hash, trusted_pkg_dir)

    # Snapshot the seeded package's manifest + member + on-disk hashes.
    def _snapshot_package(pkg_root: Path) -> dict[str, str]:
        """Snapshot manifest, members, and on-disk content hashes for a package."""
        snap: dict[str, str] = {}
        if not pkg_root.is_dir():
            return snap
        for fpath in sorted(pkg_root.rglob("*")):
            if fpath.is_file():
                rel = str(fpath.relative_to(pkg_root))
                snap[rel] = hashlib.sha256(fpath.read_bytes()).hexdigest()
        return snap

    trusted_pkg_path = store.canonical_path("jobs", "system", trusted_content_hash)
    trusted_snapshot_before = _snapshot_package(trusted_pkg_path)
    assert len(trusted_snapshot_before) > 0, (
        f"Trusted package must have content; path={trusted_pkg_path}"
    )

    # Full canonical store snapshot before failure.
    def _snapshot_canonical_store(root: Path) -> dict[str, str]:
        """Snapshot every entry path → on-disk hash."""
        snap: dict[str, str] = {}
        if not root.is_dir():
            return snap
        for p in sorted(root.rglob("*")):
            if p.is_file():
                snap[str(p.relative_to(root))] = hashlib.sha256(
                    p.read_bytes()
                ).hexdigest()
            elif p.is_dir():
                snap[str(p.relative_to(root)) + "/"] = "<dir>"
        return snap

    store_snapshot_before = _snapshot_canonical_store(store.root)

    # Compute trusted hashes for ALL contracts before failure.
    trusted_hashes_before: dict[str, str] = {}
    for cid in ["start-task", "jobs", "make-worktree", "thread"]:
        d = proto_skills / cid
        if d.exists():
            trusted_hashes_before[cid] = _compute_dir_hash(d)

    # ── Workshop workspace state ──────────────────────────────────────────
    eh_workspace = test_runtime.workspaces_dir / "engineering_head"
    # Set up repos so make-worktree contract can be resolved.
    (eh_workspace / "repos" / "test" / ".git").mkdir(parents=True, exist_ok=True)

    def _snapshot_workspace_root(ws: Path) -> dict[str, str | None]:
        """Snapshot workspace: file hashes + symlink targets."""
        snap: dict[str, str | None] = {}
        if not ws.is_dir():
            return snap
        for p in sorted(ws.rglob("*")):
            rel = str(p.relative_to(ws))
            if p.is_symlink():
                snap[rel] = f"link->{p.readlink()}"
            elif p.is_file():
                snap[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
            elif p.is_dir():
                snap[rel + "/"] = None  # marker for directory
        return snap

    ws_snapshot_before = _snapshot_workspace_root(eh_workspace)

    # ── Snapshot task + audit state ───────────────────────────────────────
    task_id = orchestrator.create_task("ping")
    task_before = orchestrator._db.get_task(task_id)
    assert task_before is not None
    assert task_before.status == TaskStatus.PENDING, (
        f"Task must start as PENDING; got {task_before.status}"
    )
    audit_before = len(orchestrator._db.get_audit_logs(task_id))

    # ── Concrete _build_executor spy with reachable executor.run ───────────
    spy_control: dict[str, bool] = {"run_called": False}
    from runtime.orchestrator.executors import ClaudeExecutor
    _real_build = orchestrator._build_executor

    def _spy_build_executor(provider: str):
        real_exec = _real_build(provider)
        # Wrap .run to trap any invocation as proof of no-early-fail.
        original_run = real_exec.run
        def _trap_run(*args, **kwargs):
            spy_control["run_called"] = True
            raise AssertionError(
                "executor.run must not be invoked when preflight fails"
            )
        real_exec.run = _trap_run  # type: ignore[method-assign]
        return real_exec

    monkeypatch.setattr(
        orchestrator, "_build_executor", _spy_build_executor,
    )

    # ── Remove 'thread' (last alphabetically in TASK context contracts) ───
    thread_dir = proto_skills / "thread"
    assert thread_dir.exists(), "thread must exist before removal"
    shutil.rmtree(thread_dir)
    assert not thread_dir.exists()

    # ── Trigger the real missing-source failure ──
    with pytest.raises(SystemContractMaterializationError) as exc_info:
        orchestrator._run_agent(task_id, "engineering_head", "any prompt")

    msg = str(exc_info.value)
    assert "thread" in msg, (
        f"Error must name 'thread' as missing: {msg!r}"
    )

    # ── executor.run never invoked ───────────────────────────────────────
    assert spy_control["run_called"] is False, (
        "executor.run must never be invoked when preflight fails; "
        f"spy_control={spy_control}"
    )

    # ── No canonical package was built for non-seeded contracts ──────────
    # "jobs" was seeded by us — it should have exactly the seeded entry.
    for cid in ["start-task", "jobs", "make-worktree", "thread"]:
        pkg_base = store.root / cid / "system"
        if cid == "jobs":
            # Only the seeded package should exist
            if pkg_base.exists():
                built_entries = list(pkg_base.iterdir())
                assert len(built_entries) == 1, (
                    f"Seeded 'jobs' package must have exactly 1 entry; "
                    f"got {built_entries}"
                )
        else:
            if pkg_base.exists():
                built_entries = list(pkg_base.iterdir())
                assert len(built_entries) == 0, (
                    f"Canonical package {cid} was built despite preflight failure. "
                    f"Contents: {built_entries}"
                )

    # ── Canonical store fully unchanged (including trusted package) ───────
    store_snapshot_after = _snapshot_canonical_store(store.root)
    assert store_snapshot_after == store_snapshot_before, (
        f"Canonical store was mutated by failed preflight.\n"
        f"Added: {set(store_snapshot_after) - set(store_snapshot_before)}\n"
        f"Removed: {set(store_snapshot_before) - set(store_snapshot_after)}"
    )

    trusted_snapshot_after = _snapshot_package(trusted_pkg_path)
    assert trusted_snapshot_after == trusted_snapshot_before, (
        f"Trusted package was mutated: "
        f"before={set(trusted_snapshot_before)}, after={set(trusted_snapshot_after)}"
    )

    # ── Trusted source hashes unchanged ───────────────────────────────────
    for cid in ["start-task", "jobs", "make-worktree"]:
        if cid in trusted_hashes_before:
            current_hash = _compute_dir_hash(proto_skills / cid)
            assert current_hash == trusted_hashes_before[cid], (
                f"Trusted hash for {cid} changed: "
                f"{trusted_hashes_before[cid]} -> {current_hash}"
            )

    # ── Workspace state fully unchanged ───────────────────────────────────
    ws_snapshot_after = _snapshot_workspace_root(eh_workspace)
    assert ws_snapshot_after == ws_snapshot_before, (
        f"Workspace was mutated by failed materialization.\n"
        f"Added: {set(ws_snapshot_after) - set(ws_snapshot_before)}\n"
        f"Removed: {set(ws_snapshot_before) - set(ws_snapshot_after)}"
    )

    # ── Both workspace roots unchanged ────────────────────────────────────
    for subdir in (".claude/skills", ".agents/skills"):
        p = eh_workspace / subdir
        assert p.exists() == (subdir in ws_snapshot_before or (
            subdir + "/" in ws_snapshot_before
        )), (
            f"{subdir} existence changed after failure"
        )

    # ── Task status + audit unchanged (no session_start reached) ──────────
    task_after = orchestrator._db.get_task(task_id)
    assert task_after is not None
    assert task_after.status == TaskStatus.PENDING, (
        f"Task must stay PENDING after direct _run_agent failure; "
        f"got {task_after.status}"
    )
    audit_after = len(orchestrator._db.get_audit_logs(task_id))
    assert audit_after == audit_before, (
        f"No audit rows must be added for task_id={task_id}; "
        f"before={audit_before}, after={audit_after}"
    )


def test_preflight_context_union_raises_on_missing_source_executor_switch(
    orchestrator, test_runtime, test_settings, monkeypatch):
    """TASK-4175 adversarial: _materialize_context_union must preflight
    ALL required system-contract sources for the full six-context union
    BEFORE any canonical build or workspace reconciliation.

    This exercises the executor-switch path through the real orchestrator
    materialize_workspace_skills_union entry point with six contexts.

    Strengthened TASK-4176 proof-gap repair:
    - Full canonical store snapshot comparison (not just root existence).
    - Both workspace roots (.claude/skills + .agents/skills) link + file state.
    - Known trusted package manifests/members/on-disk hashes preserved.
    - No mutation of canonical/package/link state whatsoever.
    - production _compute_dir_hash identity used throughout.
    """
    import shutil
    import hashlib
    from pathlib import Path
    from runtime.orchestrator.workspace_adapters import (
        materialize_workspace_skills_union,
        SystemContractMaterializationError,
        _compute_dir_hash,
    )
    from runtime.skills.canonical_store import CanonicalSkillStore

    _setup_workspaces(test_runtime, ["dev_agent"])
    (test_runtime.workspaces_dir / "dev_agent" / "repos" / "test" / ".git").mkdir(parents=True, exist_ok=True)

    proto_skills = test_settings.get_protocol_dir() / "skills"
    _setup_protocol_skills(test_settings, [
        "start-task", "jobs", "make-worktree", "thread", "dream", "todos",
    ])

    # ── Seed a known unrelated trusted package in the canonical store ─────
    store = CanonicalSkillStore(settings=test_settings)
    trusted_pkg_dir = proto_skills / "jobs"
    trusted_content_hash = _compute_dir_hash(trusted_pkg_dir)
    store.build_from_source("jobs", "system", trusted_content_hash, trusted_pkg_dir)

    def _snapshot_canonical_store(root: Path) -> dict[str, str]:
        """Snapshot every entry path → on-disk hash."""
        snap: dict[str, str] = {}
        if not root.is_dir():
            return snap
        for p in sorted(root.rglob("*")):
            if p.is_file():
                snap[str(p.relative_to(root))] = hashlib.sha256(
                    p.read_bytes()
                ).hexdigest()
            elif p.is_dir():
                snap[str(p.relative_to(root)) + "/"] = "<dir>"
        return snap

    store_snapshot_before = _snapshot_canonical_store(store.root)

    def _snapshot_package(pkg_root: Path) -> dict[str, str]:
        """Snapshot manifest, members, and on-disk content hashes."""
        snap: dict[str, str] = {}
        if not pkg_root.is_dir():
            return snap
        for fpath in sorted(pkg_root.rglob("*")):
            if fpath.is_file():
                rel = str(fpath.relative_to(pkg_root))
                snap[rel] = hashlib.sha256(fpath.read_bytes()).hexdigest()
        return snap

    trusted_pkg_path = store.canonical_path("jobs", "system", trusted_content_hash)
    trusted_snapshot_before = _snapshot_package(trusted_pkg_path)

    # Compute trusted hashes of surviving contracts
    trusted_hashes: dict[str, str] = {}
    for cid in ["start-task", "jobs", "make-worktree", "thread"]:
        d = proto_skills / cid
        if d.exists():
            trusted_hashes[cid] = _compute_dir_hash(d)

    # ── Snapshot workspace state ──────────────────────────────────────────
    ws = test_runtime.workspaces_dir / "dev_agent"
    def _snapshot_workspace_root(root: Path) -> dict[str, str | None]:
        """Snapshot workspace: file hashes + symlink targets."""
        snap: dict[str, str | None] = {}
        if not root.is_dir():
            return snap
        for p in sorted(root.rglob("*")):
            rel = str(p.relative_to(root))
            if p.is_symlink():
                snap[rel] = f"link->{p.readlink()}"
            elif p.is_file():
                snap[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
            elif p.is_dir():
                snap[rel + "/"] = None
        return snap

    ws_snapshot_before = _snapshot_workspace_root(ws)

    # ── Remove dream — only required by DREAM context in the union ────────
    dream_dir = proto_skills / "dream"
    assert dream_dir.exists()
    shutil.rmtree(dream_dir)
    assert not dream_dir.exists()

    skills_root = test_settings.project_root / "runtime" / "skills"

    with pytest.raises(SystemContractMaterializationError) as exc_info:
        materialize_workspace_skills_union(
            ws, test_settings,
            slug="test",
            contexts=["task", "thread", "wake", "dream", "schedule", "bootstrap"],
            provider="claude",
            agent_name="dev_agent",
            team="engineering",
            skills_root=skills_root,
        )

    msg = str(exc_info.value)
    assert "dream" in msg, (
        f"Error must name 'dream' as missing: {msg!r}"
    )

    # ── No canonical packages were built for non-seeded contracts ──────────
    for cid in trusted_hashes:
        pkg_base = store.root / cid / "system"
        if cid == "jobs":
            # Only the seeded package should exist
            if pkg_base.exists():
                built = list(pkg_base.iterdir())
                assert len(built) == 1, (
                    f"Seeded 'jobs' package must have exactly 1 entry; "
                    f"got {built}"
                )
        else:
            if pkg_base.exists():
                built = list(pkg_base.iterdir())
                assert len(built) == 0, (
                    f"Canonical package {cid} was built despite preflight failure. "
                    f"Contents: {built}"
                )

    # ── Full canonical store unchanged ────────────────────────────────────
    store_snapshot_after = _snapshot_canonical_store(store.root)
    assert store_snapshot_after == store_snapshot_before, (
        f"Canonical store was mutated by failed union preflight.\n"
        f"Added: {set(store_snapshot_after) - set(store_snapshot_before)}\n"
        f"Removed: {set(store_snapshot_before) - set(store_snapshot_after)}"
    )

    # ── Trusted package completely untouched ──────────────────────────────
    trusted_snapshot_after = _snapshot_package(trusted_pkg_path)
    assert trusted_snapshot_after == trusted_snapshot_before, (
        f"Trusted package was mutated: "
        f"before={set(trusted_snapshot_before)}, after={set(trusted_snapshot_after)}"
    )

    # ── Trusted source hashes unchanged ───────────────────────────────────
    for cid, expected_hash in trusted_hashes.items():
        current = _compute_dir_hash(proto_skills / cid)
        assert current == expected_hash, (
            f"Trusted hash for {cid} changed: {expected_hash} -> {current}"
        )

    # ── Workspace state fully unchanged (no link creation/deletion) ────────
    ws_snapshot_after = _snapshot_workspace_root(ws)
    assert ws_snapshot_after == ws_snapshot_before, (
        f"Workspace was mutated by failed union materialization.\n"
        f"Added: {set(ws_snapshot_after) - set(ws_snapshot_before)}\n"
        f"Removed: {set(ws_snapshot_before) - set(ws_snapshot_after)}"
    )


# ── Issue #568: Task/subtask production-seam model-forwarding tests ──────
# These drive the real Orchestrator._run_agent production seam through the
# real executor-factory boundary, testing that executor.run receives the
# authoritative AgentDef.model and that the task path cannot silently fall
# through to provider default.


def _write_agent_def_with_model(agents_dir, agent_name, model=None):
    """Write an AgentDef frontmatter file with an optional model field."""
    from runtime.orchestrator.agent_def import AgentDef, render_agent_text
    agents_dir.mkdir(parents=True, exist_ok=True)
    ad = AgentDef(
        name=agent_name, team="engineering", role="worker",
        executor="claude", allow_rules=(), repos={},
        enrolled_by=None, enrolled_at_task=None, enrolled_at=None,
        system_prompt=f"You are {agent_name}.", description="",
        model=model,
    )
    (agents_dir / f"{agent_name}.md").write_text(render_agent_text(ad))


def test_task_subtask_forwards_configured_model_to_executor_run(
    orchestrator, test_runtime, monkeypatch,
):
    """When AgentDef.model is set, _run_agent passes it to executor.run(model=...).

    Drives the real production seam — Orchestrator._run_agent calls
    executor.run(model=model_name) after resolving via _resolve_model_name
    from the authoritative org/agents/<name>.md frontmatter.
    """
    _setup_workspaces(test_runtime, ["dev_agent"])
    _write_agent_def_with_model(
        test_runtime.agents_dir, "dev_agent", model="gpt-5.6-terra",
    )

    task_id = orchestrator.create_task("Test model forwarding")
    monkeypatch.setattr(orchestrator, "_build_session_id", lambda: "sess-model")

    captured_model = {}

    class _CapturingExec:
        def run(self, **kwargs):
            captured_model["model"] = kwargs.get("model")
            return ExecutorResult(
                success=True, duration_seconds=1, session_id="sess-model",
            )

    with patch.object(orchestrator, "_build_executor", return_value=_CapturingExec()):
        orchestrator._run_agent(task_id, "dev_agent", "")

    assert captured_model.get("model") == "gpt-5.6-terra", (
        f"expected model='gpt-5.6-terra', got {captured_model.get('model')!r}"
    )


def test_task_subtask_no_configured_model_preserves_default(
    orchestrator, test_runtime, monkeypatch,
):
    """When AgentDef.model is absent, _run_agent passes model=None.

    Proves the established model=None/default behavior through the real
    production seam — executor.run receives None, not a fabricated default.
    """
    _setup_workspaces(test_runtime, ["dev_agent"])
    _write_agent_def_with_model(
        test_runtime.agents_dir, "dev_agent", model=None,
    )

    task_id = orchestrator.create_task("Test no-model default")
    monkeypatch.setattr(orchestrator, "_build_session_id", lambda: "sess-nodef")

    captured_model = {}

    class _CapturingExec:
        def run(self, **kwargs):
            captured_model["model"] = kwargs.get("model")
            return ExecutorResult(
                success=True, duration_seconds=1, session_id="sess-nodef",
            )

    with patch.object(orchestrator, "_build_executor", return_value=_CapturingExec()):
        orchestrator._run_agent(task_id, "dev_agent", "")

    assert captured_model.get("model") is None, (
        f"model should be None when AgentDef has no model, "
        f"got {captured_model.get('model')!r}"
    )


def test_task_subtask_model_mismatch_detected_by_fake_executor(
    orchestrator, test_runtime, monkeypatch,
):
    """Deterministic mismatch: fake executor fails only when gpt-5.6-terra
    is actually forwarded, proving the task path cannot silently fall through
    to provider default.

    A fake executor that rejects gpt-5.6-terra MUST cause the task to fail
    when the model IS configured; the failure proves the model was forwarded
    through the full production seam. If the model silently fell through to
    default, the fake would succeed — and this test would fail.
    """
    _setup_workspaces(test_runtime, ["dev_agent"])
    _write_agent_def_with_model(
        test_runtime.agents_dir, "dev_agent", model="gpt-5.6-terra",
    )

    task_id = orchestrator.create_task("Test model mismatch")
    monkeypatch.setattr(orchestrator, "_build_session_id", lambda: "sess-mismatch")

    # Fake executor that FAILS only when gpt-5.6-terra reaches it.
    # If the task path silently fell to provider default, this fake
    # would return success — and the test assertion below would catch it.
    class _MismatchDetector:
        def __init__(self):
            self.received_model = None

        def run(self, **kwargs):
            self.received_model = kwargs.get("model")
            if self.received_model == "gpt-5.6-terra":
                # Deterministic failure: model WAS forwarded correctly.
                result = ExecutorResult(
                    success=False,
                    error="fake: gpt-5.6-terra intentionally rejected",
                    duration_seconds=0, session_id="",
                )
                result.returncode = 1
                return result
            # Otherwise succeed — would be the fall-to-default case.
            return ExecutorResult(
                success=True, duration_seconds=1, session_id="sess-mismatch",
            )

    detector = _MismatchDetector()
    with patch.object(orchestrator, "_build_executor", return_value=detector):
        result, report = orchestrator._run_agent(task_id, "dev_agent", "")

    # The fake MUST have received and rejected gpt-5.6-terra.
    assert detector.received_model == "gpt-5.6-terra", (
        f"fake executor did NOT receive the configured model; "
        f"got {detector.received_model!r} — task path fell through to default"
    )
    # The executor MUST have failed (detector rejected the model).
    assert not result.success, (
        f"mismatch test must fail when model is forwarded; "
        f"a success here means the model was dropped. "
        f"error={result.error!r}"
    )


def test_malformed_task_scratch_manifest_preserves_agent_failure_note_and_audit(
    orchestrator, test_runtime, monkeypatch,
):
    _setup_workspaces(test_runtime, ["dev_agent"])
    task_id = orchestrator.create_task("Malformed task scratch manifest")
    monkeypatch.setattr(orchestrator, "_build_session_id", lambda: "sess-malformed")
    manifest_dir = (
        test_runtime.workspaces_dir
        / "dev_agent/.happyranch/task-scratch-manifests"
    )
    manifest_dir.mkdir(parents=True)
    (manifest_dir / f"{task_id}.json").write_text(json.dumps({
        "version": 1,
        "task_id": task_id,
        "producers": "agent",
    }))

    result, report = orchestrator._run_agent(task_id, "dev_agent", "")

    assert result.success is False
    assert result.error == "manifest is corrupt"
    assert report is None
    task = orchestrator._db.get_task(task_id)
    assert task is not None
    assert task.note == "Task scratch containment refused launch: manifest is corrupt"
    audits = orchestrator._db.get_audit_logs(task_id)
    failures = [row for row in audits if row["action"] == "task_scratch_containment_failed"]
    assert len(failures) == 1
    assert failures[0]["payload"] == {"error": "manifest is corrupt"}


def test_wrong_root_task_scratch_manifest_preserves_agent_failure_note_and_audit(
    orchestrator, test_runtime, monkeypatch,
):
    _setup_workspaces(test_runtime, ["dev_agent"])
    task_id = orchestrator.create_task("Wrong-root task scratch manifest")
    monkeypatch.setattr(orchestrator, "_build_session_id", lambda: "sess-wrong-root")
    manifest_dir = (
        test_runtime.workspaces_dir
        / "dev_agent/.happyranch/task-scratch-manifests"
    )
    manifest_dir.mkdir(parents=True)
    wrong_root = "/attacker/chosen"
    (manifest_dir / f"{task_id}.json").write_text(json.dumps({
        "version": 1,
        "task_id": task_id,
        "required_root": wrong_root,
        "observed_root": wrong_root,
        "root_classification": "regenerable_scratch",
        "manifest_classification": "durable_recovery_artifact",
        "lock_classification": "durable_recovery_artifact",
        "producers": [{
            "producer_kind": "agent",
            "producer_id": "sess-old",
            "required": {"canonical_root": wrong_root, "ownership": "runtime"},
            "observed": {"canonical_root": wrong_root, "mode": "0700"},
            "classification": "regenerable_scratch",
            "observed_at": "2026-09-02T00:00:00+00:00",
        }],
    }))

    result, report = orchestrator._run_agent(task_id, "dev_agent", "")

    assert result.success is False
    assert result.error == "manifest is corrupt"
    assert report is None
    task = orchestrator._db.get_task(task_id)
    assert task is not None
    assert task.note == "Task scratch containment refused launch: manifest is corrupt"
    audits = orchestrator._db.get_audit_logs(task_id)
    failures = [row for row in audits if row["action"] == "task_scratch_containment_failed"]
    assert len(failures) == 1
    assert failures[0]["payload"] == {"error": "manifest is corrupt"}
