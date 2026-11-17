import copy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_collaboration import create_episode, create_project
from test_story_bible import archive as archive_character
from test_story_bible import create as create_character
from test_story_bible import replace as replace_character
from test_story_outline import confirm, derive, execute, generate, path, save

from arclight.application.agent_memory_sources import capture_memory_sources
from arclight.application.context_sources import capture_sources, validate_sources
from arclight.domain.versions import digest
from arclight.persistence.models import ProjectMember, StoryOutline, WorkflowRun


def series_content(outline, characters):
    first, second = characters
    return {
        **copy.deepcopy(outline["content"]),
        "schema_version": "2.0",
        "series_plan": {
            "schema_version": "1.0",
            "kind": "PLANNED",
            "threads": [
                {
                    "id": "letter",
                    "kind": "MAIN",
                    "title": "送达失落的信",
                    "central_conflict": "阿青必须送信，但信中的家族秘密威胁收信人。",
                    "planned_resolution": "第三集公开真相，由收信人选择信的归属。",
                },
                {
                    "id": "trust",
                    "kind": "SUBPLOT",
                    "title": "从防备到合作",
                    "central_conflict": "石岚怀疑阿青利用信件牟利。",
                    "planned_resolution": "两人在送信行动中建立信任。",
                },
            ],
            "character_arcs": [
                {
                    "character_id": first["id"],
                    "character_revision": first["revision"],
                    "starting_goal": "尽快送信换取路费。",
                    "internal_conflict": "阿青不愿承担陌生人的风险。",
                    "planned_growth": "从只求自保到尊重收信人的决定。",
                    "milestones": [
                        {"episode_no": 1, "change": "发现信件可能引来追查。"},
                        {"episode_no": 2, "change": "放弃卖信，主动保护收信人。"},
                        {"episode_no": 3, "change": "交出信件并接受对方的选择。"},
                    ],
                },
                {
                    "character_id": second["id"],
                    "character_revision": second["revision"],
                    "starting_goal": "夺回涉及家族的信件。",
                    "internal_conflict": "石岚把保守秘密视为唯一的保护。",
                    "planned_growth": "学会让家人共同面对事实。",
                    "milestones": [
                        {"episode_no": 1, "change": "试图独自截信。"},
                        {"episode_no": 3, "change": "允许家人亲自阅读原信。"},
                    ],
                },
            ],
            "relationship_arcs": [
                {
                    "id": "qing-lan",
                    "source_character_id": first["id"],
                    "target_character_id": second["id"],
                    "initial_relationship": "双方互相戒备。",
                    "planned_development": "通过共同护送，成为能够坦诚合作的伙伴。",
                    "milestones": [
                        {"episode_no": 2, "change": "石岚看到阿青放弃卖信，决定协助。"},
                        {"episode_no": 3, "change": "两人共同守护收信人的选择。"},
                    ],
                }
            ],
            "foreshadowing": [
                {
                    "id": "red-seal",
                    "thread_id": "letter",
                    "setup_episode_no": 1,
                    "setup": "信封的红印与石岚随身旧物相同。",
                    "expected_payoff_episode_no": 3,
                    "planned_payoff": "旧物与红印证明信来自石岚失散的母亲。",
                }
            ],
            "episode_progressions": [
                {
                    "episode_no": number,
                    "thread_beats": [
                        {"thread_id": "letter", "progression": main},
                        {"thread_id": "trust", "progression": subplot},
                    ],
                    "caused_by_episode_nos": [] if number == 1 else [number - 1],
                    "causal_link": cause,
                    "ending_handoff": handoff,
                }
                for number, main, subplot, cause, handoff in [
                    (
                        1,
                        "捡信并认出红印。",
                        "石岚拦路，双方互疑。",
                        "雨夜误捡信引出追查。",
                        "追查者抵达渡口，迫使两人同行。",
                    ),
                    (
                        2,
                        "护送中发现信的收件人。",
                        "放弃卖信的选择赢得合作。",
                        "因渡口被封，两人寻找旧路。",
                        "收件人拒绝开门，留下身份疑问。",
                    ),
                    (
                        3,
                        "红印线索指向失散母亲，完成交信。",
                        "两人尊重家人的共同决定。",
                        "旧路带来的家族线索说服收件人。",
                        "信已送达，家人自行决定如何回应。",
                    ),
                ]
            ],
        },
    }


@pytest.fixture
def planned_outline(client, episode, settings):
    characters = [
        create_character(client, episode, "CHARACTER", name="阿青", description="年轻信使"),
        create_character(client, episode, "CHARACTER", name="石岚", description="守护家人的旅人"),
    ]
    outline = generate(
        client,
        episode,
        settings,
        {
            "story": "阿青捡到一封信，与石岚共同寻找收件人。",
            "target_episode_count": 3,
            "episode_duration_ms": 30000,
        },
    )
    return outline, characters, series_content(outline, characters)


def test_series_plan_reuses_outline_versions_and_exact_owner_confirmation(
    client, episode, planned_outline
):
    old, characters, content = planned_outline
    response = save(client, episode, old, content, "series-plan")
    assert response.status_code == 201, response.text
    planned = response.json()
    assert planned["base_version_id"] == old["id"] and planned["version_no"] == 2
    assert planned["content"] == content and planned["status"] == "DRAFT"
    source = planned["source_snapshot"]
    assert {k: v for k, v in source.items() if k != "series_plan_references"} == old[
        "source_snapshot"
    ]
    references = source["series_plan_references"]
    assert references["kind"] == "PLANNED"
    assert [(r["id"], r["revision"], r["name"]) for r in references["characters"]] == [
        (r["id"], r["revision"], r["name"]) for r in characters
    ]
    assert all(len(r["sha256"]) == 64 for r in references["characters"])
    approved_response = confirm(client, episode, planned)
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()
    assert (
        approved["status"] == "APPROVED" and approved["confirmed_by"] and approved["confirmed_at"]
    )
    assert confirm(client, episode, planned).json() == approved
    assert save(client, episode, old, content, "series-plan").json() == approved
    changed = copy.deepcopy(content)
    changed["series_plan"]["foreshadowing"][0]["planned_payoff"] = (
        "阿青将红印与旧物并置，请石岚自己辨认母亲的笔迹。"
    )
    updated_response = save(client, episode, approved, changed, "local-foreshadow-edit")
    assert updated_response.status_code == 201, updated_response.text
    updated = updated_response.json()
    assert updated["status"] == "DRAFT" and updated["base_version_id"] == approved["id"]
    assert updated["content"]["episodes"] == content["episodes"]
    for field in ("threads", "character_arcs", "relationship_arcs", "episode_progressions"):
        assert updated["content"]["series_plan"][field] == content["series_plan"][field]
    assert client.get(path(episode) + "/story-outlines/" + approved["id"]).json() == approved
    assert client.get(path(episode) + "/story-outlines/" + old["id"]).json() == old
    assert "series_plan" not in old["content"]


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p["threads"][0].update(kind="SUBPLOT"),
        lambda p: p["threads"].append(copy.deepcopy(p["threads"][0])),
        lambda p: p["character_arcs"].append(copy.deepcopy(p["character_arcs"][0])),
        lambda p: p["character_arcs"][0].update(character_revision=True),
        lambda p: p["character_arcs"][0]["milestones"][0].update(episode_no=4),
        lambda p: p["relationship_arcs"][0].update(target_character_id=str(uuid4())),
        lambda p: p["relationship_arcs"][0].update(
            target_character_id=p["relationship_arcs"][0]["source_character_id"]
        ),
        lambda p: p["foreshadowing"][0].update(thread_id="unknown"),
        lambda p: p["foreshadowing"][0].update(setup_episode_no=3, expected_payoff_episode_no=2),
        lambda p: p["foreshadowing"][0].update(expected_payoff_episode_no=4),
        lambda p: p["episode_progressions"].pop(),
        lambda p: p["episode_progressions"][1].update(caused_by_episode_nos=[2]),
        lambda p: p["episode_progressions"][1].update(caused_by_episode_nos=[4]),
        lambda p: p["episode_progressions"][1].update(caused_by_episode_nos=[3]),
        lambda p: p["episode_progressions"][0]["thread_beats"][0].update(thread_id="unknown"),
        lambda p: p.update(kind="CONFIRMED_FACT"),
        lambda p: p["threads"][0].update(central_conflict="   "),
    ],
)
def test_series_plan_rejects_invalid_cross_references_atomically(
    client, episode, planned_outline, change
):
    old, _, content = planned_outline
    change(content["series_plan"])
    response = save(client, episode, old, content)
    assert response.status_code == 422, response.text
    assert client.get(path(episode) + "/story-outlines").json()[0]["id"] == old["id"]
    assert client.get(path(episode) + "/story-outlines/" + old["id"]).json() == old


@pytest.mark.parametrize("problem", ["missing", "foreign", "wrong_kind", "archived", "stale"])
def test_series_characters_are_current_same_project_stable_identities(
    client, episode, settings, planned_outline, problem
):
    old, characters, content = planned_outline
    first = characters[0]
    if problem == "missing":
        content["series_plan"]["character_arcs"][0]["character_id"] = str(uuid4())
        content["series_plan"]["relationship_arcs"] = []
    elif problem == "foreign":
        project = create_project(client)
        other = create_episode(client, project)
        foreign = create_character(client, other, "CHARACTER", name="同名人物")
        content["series_plan"]["character_arcs"][0]["character_id"] = foreign["id"]
        content["series_plan"]["relationship_arcs"] = []
    elif problem == "wrong_kind":
        prop = create_character(client, episode, "PROP", name="信件")
        content["series_plan"]["character_arcs"][0]["character_id"] = prop["id"]
        content["series_plan"]["relationship_arcs"] = []
    elif problem == "archived":
        assert archive_character(client, episode, first).status_code == 200
    else:
        assert (
            replace_character(client, episode, first, description="修订后的人物").status_code == 200
        )
    response = save(client, episode, old, content)
    assert response.status_code == (409 if problem == "stale" else 422), response.text
    assert response.json()["error"]["code"] == (
        "SERIES_CHARACTER_STALE" if problem == "stale" else "SERIES_CHARACTER_NOT_FOUND"
    )
    assert len(client.get(path(episode) + "/story-outlines").json()) == 1


def test_series_confirmation_rechecks_snapshot_and_keeps_exact_replay(
    client, episode, planned_outline
):
    old, characters, content = planned_outline
    planned = save(client, episode, old, content).json()
    revised = replace_character(client, episode, characters[0], name="阿青的新名字").json()
    assert save(client, episode, old, content).json() == planned
    rejected = confirm(client, episode, planned)
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "SERIES_CHARACTER_STALE"
    content["series_plan"]["character_arcs"][0]["character_revision"] = revised["revision"]
    current = save(client, episode, planned, content, "refreshed-character").json()
    approved = confirm(client, episode, current).json()
    assert approved["status"] == "APPROVED"
    assert archive_character(client, episode, revised).status_code == 200
    assert confirm(client, episode, approved).json() == approved
    assert client.get(path(episode) + "/story-outlines/" + approved["id"]).json() == approved
    assert planned["source_snapshot"]["series_plan_references"]["characters"][0]["name"] == "阿青"


def test_approved_series_outline_keeps_full_context_hash_but_never_becomes_fact(
    client, episode, planned_outline
):
    old, _, content = planned_outline
    planned = save(client, episode, old, content).json()
    approved = confirm(client, episode, planned).json()
    project_id = UUID(episode["project_id"])
    context = {"required": {"global": {}}}
    with client.app.state.session_factory() as db:
        sources = capture_sources(
            db,
            project_id,
            {
                "outline_context": {
                    "version_id": approved["id"],
                    "source_snapshot": approved["source_snapshot"],
                }
            },
            context,
        )
        outline_source = next(s for s in sources if s["kind"] == "OUTLINE")
        assert outline_source["sha256"] == digest(content)
        assert outline_source["sha256"] != digest(old["content"])
        validate_sources(db, project_id, sources)
        memory = capture_memory_sources(db, project_id, sources)["memory_sources"]
        assert memory[0]["category"] == "EVIDENCE"
        assert memory[0].get("confirmation_rule") is None
        assert memory[0]["source_sha256"] == digest(outline_source)
    old_approved = confirm(client, episode, old).json()
    with client.app.state.session_factory() as db:
        legacy_sources = capture_sources(
            db,
            project_id,
            {
                "outline_context": {
                    "version_id": old_approved["id"],
                    "source_snapshot": old_approved["source_snapshot"],
                }
            },
            context,
        )
        memory = capture_memory_sources(db, project_id, legacy_sources)["memory_sources"]
        assert memory[0]["category"] == "DOMAIN_FACT"
        assert memory[0]["confirmation_rule"] == "CONFIRMED_OUTLINE"


def test_series_outline_derives_existing_single_episode_writer_without_replacing_inputs(
    client, episode, settings, planned_outline
):
    old, _, content = planned_outline
    planned = save(client, episode, old, content).json()
    approved = confirm(client, episode, planned).json()
    response = derive(client, episode, approved)
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["run_id"])
    with client.app.state.session_factory() as db:
        original = copy.deepcopy(db.get(WorkflowRun, run_id).input_json["outline_context"])
    assert original["version_id"] == approved["id"]
    assert original["episode"] == content["episodes"][0]
    assert original["source_snapshot"] == approved["source_snapshot"]
    execute(client, settings, str(run_id))
    with client.app.state.session_factory() as db:
        assert db.get(WorkflowRun, run_id).input_json["outline_context"] == original
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "WAITING_REVIEW"


def test_series_plan_editor_viewer_archive_and_cross_project_permissions(client, settings):
    from test_project_deletion import archive, create_episode, create_project, register

    owner = register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    characters = [
        create_character(client, episode, "CHARACTER", name=name) for name in ("阿青", "石岚")
    ]
    old = generate(
        client,
        episode,
        settings,
        {"story": "送信故事", "target_episode_count": 3, "episode_duration_ms": 30000},
    )
    content = series_content(old, characters)
    planned = save(client, episode, old, content).json()
    member = register(client, "series-viewer@example.com")
    with client.app.state.session_factory.begin() as db:
        db.add(
            ProjectMember(
                project_id=UUID(project["id"]), user_id=UUID(member["user"]["id"]), role="viewer"
            )
        )
    assert client.get(path(episode) + "/story-outlines/" + planned["id"]).status_code == 200
    assert save(client, episode, old, content, "viewer-write").status_code == 403
    assert confirm(client, episode, planned).status_code == 403
    with client.app.state.session_factory.begin() as db:
        db.scalar(
            select(ProjectMember).where(ProjectMember.user_id == UUID(member["user"]["id"]))
        ).role = "editor"
    assert save(client, episode, planned, content, "editor-write").status_code == 201
    assert confirm(client, episode, planned).status_code == 403
    register(client, "series-outsider@example.com")
    assert client.get(path(episode) + "/story-outlines/" + planned["id"]).status_code == 404
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    archive(client, project)
    assert client.get(path(episode) + "/story-outlines/" + planned["id"]).status_code == 200
    assert save(client, episode, planned, content, "archived-write").status_code == 409
    assert confirm(client, episode, planned).status_code == 409
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(StoryOutline)) == 3


def test_series_plan_cannot_be_silently_downgraded(client, episode, planned_outline):
    old, _, content = planned_outline
    planned = save(client, episode, old, content).json()
    rejected = save(client, episode, planned, old["content"], "downgrade")
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error"]["code"] == "SERIES_PLAN_REQUIRED"
    rows = client.get(path(episode) + "/story-outlines").json()
    assert {row["id"] for row in rows} == {old["id"], planned["id"]}
    assert client.get(path(episode) + "/story-outlines/" + planned["id"]).json() == planned


@pytest.mark.parametrize("change", ["revision", "archive", "unversioned_data"])
def test_series_approved_derivation_rechecks_characters_and_keeps_accepted_replay(
    client, episode, planned_outline, change
):
    from arclight.persistence.models import StoryEntry

    old, characters, content = planned_outline
    planned = save(client, episode, old, content).json()
    approved = confirm(client, episode, planned).json()
    accepted = derive(client, episode, approved, "accepted-series")
    assert accepted.status_code == 202, accepted.text
    run_id = UUID(accepted.json()["run_id"])
    with client.app.state.session_factory.begin() as db:
        run = db.get(WorkflowRun, run_id)
        frozen_input = copy.deepcopy(run.input_json)
        run.status = "CANCELLED"
    if change == "revision":
        assert (
            replace_character(client, episode, characters[0], name="修订后名字").status_code == 200
        )
    elif change == "archive":
        assert archive_character(client, episode, characters[0]).status_code == 200
    else:
        with client.app.state.session_factory.begin() as db:
            entry = db.get(StoryEntry, UUID(characters[0]["id"]))
            entry.data_json = {**entry.data_json, "description": "修订号未变化的正文漂移"}
    replayed = derive(client, episode, approved, "accepted-series")
    assert replayed.status_code == 202, replayed.text
    assert replayed.json()["run_id"] == str(run_id)
    rejected = derive(client, episode, approved, "new-series")
    assert rejected.status_code == (422 if change == "archive" else 409), rejected.text
    assert rejected.json()["error"]["code"] == (
        "SERIES_CHARACTER_NOT_FOUND" if change == "archive" else "SERIES_CHARACTER_STALE"
    )
    assert confirm(client, episode, approved).json() == approved
    assert client.get(path(episode) + "/story-outlines/" + approved["id"]).json() == approved
    with client.app.state.session_factory() as db:
        assert db.get(WorkflowRun, run_id).input_json == frozen_input
        assert (
            db.scalar(
                select(func.count())
                .select_from(WorkflowRun)
                .where(WorkflowRun.episode_id == UUID(episode["id"]))
            )
            == 2
        )


@pytest.mark.parametrize("operation", ["save", "confirm", "derive"])
def test_postgres_series_character_revision_is_refreshed_after_project_gate(
    client, episode, settings, planned_outline, operation
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import event
    from test_concurrency import require_postgres

    from arclight.application.commands import start_run
    from arclight.application.story_outline import confirm_outline, save_outline
    from arclight.core.errors import DomainError
    from arclight.domain.series_planning import SeriesOutlineContent
    from arclight.persistence.models import Project, StoryEntry

    require_postgres(settings)
    old, characters, content = planned_outline
    planned = save(client, episode, old, content).json()
    if operation == "derive":
        planned = confirm(client, episode, planned).json()
    factory = client.app.state.session_factory
    gate_attempted = Event()

    def stale_writer():
        with factory.begin() as db:
            cached = db.get(StoryEntry, UUID(characters[0]["id"]))
            assert cached.revision == characters[0]["revision"]

            def observe_gate(connection, cursor, statement, parameters, context, executemany):
                if "FROM projects" in statement and "FOR UPDATE" in statement:
                    gate_attempted.set()

            event.listen(db.connection(), "before_cursor_execute", observe_gate)
            with pytest.raises(DomainError) as error:
                if operation == "save":
                    save_outline(
                        db,
                        UUID(episode["id"]),
                        "service-owner",
                        UUID(planned["id"]),
                        SeriesOutlineContent.model_validate(content),
                        "pg-stale-series",
                    )
                elif operation == "confirm":
                    confirm_outline(db, UUID(episode["id"]), "service-owner", UUID(planned["id"]))
                else:
                    start_run(
                        db,
                        UUID(episode["id"]),
                        "service-owner",
                        "",
                        "pg-stale-series",
                        "test",
                        settings=settings,
                        outline_version_id=UUID(planned["id"]),
                    )
            return error.value.code

    with ThreadPoolExecutor(max_workers=1) as pool:
        with factory.begin() as db:
            db.get(Project, UUID(episode["project_id"]), with_for_update=True)
            revised = db.get(StoryEntry, UUID(characters[0]["id"]))
            revised.name = "并发修改后的人物"
            revised.revision += 1
            db.flush()
            future = pool.submit(stale_writer)
            assert gate_attempted.wait(10)
        assert future.result(timeout=15) == "SERIES_CHARACTER_STALE"
    detail = client.get(path(episode) + "/story-outlines/" + planned["id"]).json()
    assert detail["status"] == ("APPROVED" if operation == "derive" else "DRAFT")
    assert len(client.get(path(episode) + "/story-outlines").json()) == 2
