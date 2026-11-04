"""GEN-11: exact definitions, persisted provenance and compatibility before execution."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError
from test_api_workflow import advance, review, start
from test_collaboration import register
from test_generation_delivery import Provider, deliver
from test_generation_jobs import capture_ok

from arclight.api.production_versions import VersionRead
from arclight.application import production_versions as versions
from arclight.application.production_versions import bind, capture_text, text_settings
from arclight.core.errors import DomainError
from arclight.persistence.models import (
    GenerationAttempt,
    ProductionVersion,
    Script,
    VersionBinding,
    WorkflowRun,
)
from arclight.providers.prompts import TEXT_PROMPTS


def url(episode):
    return f"/api/v1/projects/{episode['project_id']}/production-versions"


def bindings(client, episode, resource_id=None, kind=None):
    params = {"limit": 100}
    if resource_id:
        params["resource_id"] = str(resource_id)
    if kind:
        params["resource_type"] = kind
    response = client.get(url(episode) + "/bindings", params=params)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("family", ["TEXT", "ART", "REVIEW", "MEDIA"])
@pytest.mark.parametrize("kind", ["AGENT", "PROMPT", "SCHEMA", "MODEL"])
def test_all_production_families_have_queryable_content_addressed_versions(
    client, episode, family, kind
):
    body = {
        "family": family,
        "kind": kind,
        "name": "fixture",
        "version": "1",
        "definition": {"schema_version": 1, "definition_source": "contract-fixture"},
    }
    first = client.post(url(episode), json=body)
    assert first.status_code == 201, first.text
    entry = first.json()
    assert client.post(url(episode), json=body).json()["id"] == entry["id"]
    assert entry["digest"] == versions.digest(body["definition"])
    assert client.get(url(episode) + "/" + entry["id"]).json() == entry
    assert client.get(url(episode), params={"family": family, "kind": kind}).json() == [entry]
    body["definition"]["schema_version"] = 2
    second = client.post(url(episode), json=body).json()
    assert second["id"] != entry["id"] and second["digest"] != entry["digest"]
    assert client.get(url(episode) + "/" + entry["id"]).json() == entry
    assert not bindings(client, episode), "Registration must not invent execution evidence"


def test_text_run_and_both_artifacts_bind_actual_templates_schemas_and_parameters(
    client, episode, settings
):
    run_id = start(client, episode).json()["run_id"]
    initial = bindings(client, episode, run_id)[0]["snapshot_json"]
    assert initial["workflow_type"] == "script_storyboard"
    assert initial["parameters"]["max_tokens"] == 6000
    assert initial["model"]["provider"] == "mock"
    prompts = client.get(url(episode), params={"kind": "PROMPT"}).json()
    assert {row["definition_json"]["system_template"] for row in prompts} == {
        TEXT_PROMPTS["script"],
        TEXT_PROMPTS["storyboard"],
    }
    state = advance(client, settings, run_id)
    assert state["status"] == "WAITING_REVIEW", state
    script_id = state["pending_review"]["version_id"]
    script_binding = bindings(client, episode, script_id, "SCRIPT")[0]["snapshot_json"]
    assert script_binding["components"] == initial["components"]
    assert script_binding["source_id"] == run_id
    assert review(client, state).status_code == 202
    state = advance(client, settings, run_id)
    assert state["status"] == "WAITING_REVIEW", state
    assert bindings(client, episode, state["pending_review"]["version_id"], "STORYBOARD")
    assert bindings(client, episode, run_id)[0]["snapshot_json"] == initial


@pytest.mark.parametrize("timezone", ["Asia/Shanghai", "America/New_York"])
def test_postgres_session_timezone_does_not_change_version_receipt(client, episode, timezone):
    body = {
        "family": "TEXT",
        "kind": "AGENT",
        "name": "timezone-fixture",
        "version": "1",
        "definition": {"source": "contract-fixture"},
    }
    entry = client.post(url(episode), json=body).json()
    with client.app.state.session_factory.begin() as db:
        if db.get_bind().dialect.name != "postgresql":
            pytest.skip("Requires PostgreSQL timestamp with timezone semantics")
        db.execute(text("SELECT set_config('TimeZone', :zone, true)"), {"zone": timezone})
        row = db.get(ProductionVersion, UUID(entry["id"]))
        assert VersionRead.model_validate(row).model_dump(mode="json") == entry


@pytest.mark.parametrize(
    "endpoint,kind", [("story-analysis-runs", "ANALYSIS"), ("story-outline-runs", "OUTLINE")]
)
def test_analysis_and_outline_bind_their_independent_output_versions(
    client, episode, settings, endpoint, kind
):
    accepted = client.post(
        f"/api/v1/episodes/{episode['id']}/{endpoint}",
        json={"story": "一个人找到了一封信。"},
        headers={"Idempotency-Key": "version-test"},
    )
    assert accepted.status_code == 202, accepted.text
    run_id = accepted.json()["run_id"]
    assert advance(client, settings, run_id)["status"] == "SUCCEEDED"
    contract = client.get(f"/api/v1/runs/{run_id}").json()["contract"]
    assert contract["task_type"] == ("story_analysis" if kind == "ANALYSIS" else "story_outline")
    assert contract["result"]["status"] == "SUCCEEDED"
    assert contract["result"]["requires_human_review"] == (kind == "OUTLINE")
    assert contract["result"]["artifact_ids"]
    outputs = bindings(client, episode, kind=kind)
    assert len(outputs) == 1 and outputs[0]["snapshot_json"]["source_id"] == run_id


@pytest.mark.parametrize("operation", ["director", "rewrite"])
def test_director_and_rewrite_have_specific_templates_and_precise_source_versions(
    client, episode, settings, operation
):
    from test_director_workflow import script
    from test_script_rewrite import body, source
    from test_script_rewrite import start as rewrite

    if operation == "director":
        base = script(client, f"/api/v1/episodes/{episode['id']}")
        accepted = client.post(
            f"/api/v1/episodes/{episode['id']}/storyboard-runs",
            json={"script_version_id": base["id"]},
            headers={"Idempotency-Key": "director-versions"},
        )
        expected = "storyboard"
    else:
        base = source(client, episode)
        accepted = rewrite(client, episode, body(base))
        expected = "rewrite"
    assert accepted.status_code == 202, accepted.text
    run_id = accepted.json()["run_id"]
    snapshot = bindings(client, episode, run_id)[0]["snapshot_json"]
    assert {row["name"] for row in snapshot["components"] if row["kind"] == "PROMPT"} == {expected}
    assert base["id"] in str(snapshot["inputs"])
    state = advance(client, settings, run_id)
    assert state["status"] == "WAITING_REVIEW", state
    assert bindings(client, episode, state["pending_review"]["version_id"])
    contract = state["contract"]
    assert contract["task_type"] == ("storyboard" if operation == "director" else "script_rewrite")
    assert base["id"] in {entry["value"] for entry in contract["input_snapshot"]["references"]}
    assert contract["result"]["artifact_ids"] == [state["pending_review"]["version_id"]]


@pytest.mark.parametrize("changed", ["prompt", "schema", "agent"])
def test_incompatible_definitions_fail_before_provider_or_artifact_writes(
    client, episode, settings, monkeypatch, changed
):
    run_id = start(client, episode).json()["run_id"]
    before = deepcopy(bindings(client, episode, run_id))
    original = versions.text_definitions

    def incompatible(stage, workflow_version):
        rows = deepcopy(original(stage, workflow_version))
        index = {"agent": 0, "prompt": 1, "schema": 2}[changed]
        kind, version, definition = rows[index]
        rows[index] = (kind, version, {**definition, "changed_definition": True})
        return rows

    monkeypatch.setattr(versions, "text_definitions", incompatible)
    state = advance(client, settings, run_id)
    assert state["status"] == "FAILED" and state["error_code"] == "PRODUCTION_VERSION_INCOMPATIBLE"
    assert state["contract"]["versions"]["state"] == "INCOMPATIBLE"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Script)) == 0
    assert bindings(client, episode, run_id) == before
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
    monkeypatch.setattr(versions, "text_definitions", original)
    assert advance(client, settings, run_id)["status"] == "WAITING_REVIEW"


def test_model_parameters_and_exact_inputs_survive_server_configuration_changes(
    client, episode, settings
):
    run_id = start(client, episode).json()["run_id"]
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, UUID(run_id))
        changed = settings.model_copy(
            update={
                "llm_model": "another-model",
                "text_duration_tolerance_percent": 0,
                "llm_timeout_seconds": 7,
            }
        )
        restored = text_settings(db, UUID(episode["project_id"]), run, changed)
        assert restored.llm_model == settings.llm_model
        assert restored.llm_timeout_seconds == settings.llm_timeout_seconds
        assert restored.text_duration_tolerance_percent == settings.text_duration_tolerance_percent
        public = client.get(f"/api/v1/runs/{run_id}").json()["contract"]["versions"]
        assert public["model"]["model"] == settings.llm_model
        assert (
            public["parameter_sha256"]
            == bindings(client, episode)[0]["snapshot_json"]["parameter_sha256"]
        )
        assert "base_url" not in str(public) and "system_template" not in str(public)
        assert "encrypted_key" not in str(bindings(client, episode))
        source = versions.input_summary(run.input_json)
        assert source["sha256"] and source["references"]
        reversed_keys = dict(reversed(list(run.input_json.items())))
        assert versions.input_summary(reversed_keys) == source


def test_media_job_attempt_and_result_share_frozen_component_and_input_versions(client, episode):
    job = capture_ok(client, episode["project_id"])
    before = bindings(client, episode, job["id"], "GENERATION")[0]["snapshot_json"]
    assert before["input_manifest_id"] == job["input_manifest_id"]
    assert before["parameters"]["seed"] == 17
    provider = Provider()
    assert deliver(client, job, provider) == "SUCCEEDED"
    assert deliver(client, job, provider) == "SUCCEEDED" and len(provider.requests) == 1
    with client.app.state.session_factory() as db:
        attempt = db.scalar(select(GenerationAttempt))
        identity = attempt.id
    for kind in ("ATTEMPT", "RESULT"):
        result = bindings(client, episode, identity, kind)[0]["snapshot_json"]
        assert result["components"] == before["components"]
        assert result["input_manifest_id"] == before["input_manifest_id"]


def test_incompatible_media_schema_is_rejected_before_attempt(client, episode, monkeypatch):
    job = capture_ok(client, episode["project_id"])
    from arclight.domain.generation_requests import GenerationResult

    original = GenerationResult.model_json_schema
    monkeypatch.setattr(
        GenerationResult,
        "model_json_schema",
        lambda **kwargs: {**original(**kwargs), "changed": True},
    )
    provider = Provider()
    assert deliver(client, job, provider) == "FAILED" and not provider.requests
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationAttempt)) == 0


@pytest.mark.parametrize("table", [ProductionVersion, VersionBinding])
@pytest.mark.parametrize("action", ["update", "delete", "replace_or_truncate"])
def test_database_guards_preserve_version_definitions_and_bindings(client, episode, table, action):
    capture_ok(client, episode["project_id"])
    factory = client.app.state.session_factory
    with factory() as db:
        row = db.scalar(select(table))
        identity = row.id
        values = {column.name: getattr(row, column.name) for column in table.__table__.columns}
    with pytest.raises(DBAPIError), factory.begin() as db:
        if action == "update":
            db.execute(update(table).where(table.id == identity).values(project_id=uuid4()))
        elif action == "delete":
            db.execute(delete(table).where(table.id == identity))
        elif db.get_bind().dialect.name == "postgresql":
            db.execute(text(f"TRUNCATE {table.__tablename__} CASCADE"))
        else:
            db.execute(table.__table__.insert().prefix_with("OR REPLACE").values(**values))
    with factory() as db:
        assert db.get(table, identity) is not None


def test_version_history_blocks_destructive_downgrade(client, episode, migrated):
    capture_ok(client, episode["project_id"])
    # GEN-13's later migration now preserves the captured quote before reaching 0035.
    with pytest.raises(RuntimeError, match="history"):
        command.downgrade(migrated, "0034")
    command.check(migrated)


def test_registry_query_pagination_project_isolation_and_secret_rejection(client, episode):
    capture_ok(client, episode["project_id"])
    first = client.get(url(episode), params={"limit": 2}).json()
    second = client.get(url(episode), params={"limit": 2, "offset": 2}).json()
    assert len(first) == len(second) == 2
    assert not ({x["id"] for x in first} & {x["id"] for x in second})
    other = client.post("/api/v1/projects", json={"name": "其他项目"}).json()
    assert (
        client.get(
            url(episode).replace(episode["project_id"], other["id"]) + "/" + first[0]["id"]
        ).status_code
        == 404
    )
    response = client.post(
        url(episode),
        json={
            "family": "ART",
            "kind": "MODEL",
            "name": "bad",
            "version": "1",
            "definition": {"api_key": "canary"},
        },
    )
    assert response.status_code == 422 and "canary" not in response.text
    register(client, "version-outsider@example.com")
    assert client.get(url(episode)).status_code == 404
    assert client.get(url(episode) + "/bindings").status_code == 404


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_project_roles_control_registration_but_allow_history_reads(client, episode, role):
    from arclight.persistence.models import ProjectMember

    capture_ok(client, episode["project_id"])
    member = register(client, f"version-{role}@example.com")
    with client.app.state.session_factory.begin() as db:
        db.add(
            ProjectMember(
                project_id=UUID(episode["project_id"]),
                user_id=UUID(member["user"]["id"]),
                role=role,
            )
        )
    assert client.get(url(episode)).status_code == 200
    assert client.get(url(episode) + "/bindings").status_code == 200
    response = client.post(
        url(episode),
        json={
            "family": "ART",
            "kind": "AGENT",
            "name": "art-fixture",
            "version": "1",
            "definition": {"source": "fixture"},
        },
    )
    assert response.status_code == (201 if role == "editor" else 403)


def test_binding_cannot_be_reassigned_and_legacy_absence_is_explicit(client, episode, settings):
    run_id = start(client, episode).json()["run_id"]
    with client.app.state.session_factory.begin() as db:
        run = db.get(WorkflowRun, UUID(run_id))
        capture_text(db, UUID(episode["project_id"]), run, settings)
        with pytest.raises(DomainError, match="历史版本绑定"):
            bind(db, UUID(episode["project_id"]), "WORKFLOW", run.id, {"different": True})
        unknown = uuid4()
        assert versions.find_binding(db, UUID(episode["project_id"]), "WORKFLOW", unknown) is None


def test_upgrade_preserves_legacy_run_without_inventing_current_version_evidence(
    client, episode, settings, migrated, monkeypatch
):
    original = versions.capture_text
    monkeypatch.setattr(versions, "capture_text", lambda *args, **kwargs: None)
    run_id = start(client, episode).json()["run_id"]
    with client.app.state.session_factory.begin() as db:
        run = db.get(WorkflowRun, UUID(run_id))
        run.agent_state_json = None
        before = deepcopy(run.input_json)
    command.downgrade(migrated, "0034")
    command.upgrade(migrated, "head")
    monkeypatch.setattr(versions, "capture_text", original)
    assert not bindings(client, episode, run_id)
    assert advance(client, settings, run_id)["status"] == "WAITING_REVIEW"
    assert not bindings(client, episode, run_id)
    with client.app.state.session_factory() as db:
        assert db.get(WorkflowRun, UUID(run_id)).input_json == before
