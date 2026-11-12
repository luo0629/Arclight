"""Immutable voice versions and native parameter admission without provider I/O."""

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from test_model_services import account
from test_native_media_integration import configure, project

from arclight.application.audio_voices import (
    create_voice_version,
    read_voice,
    require_voice,
    voice_spec,
)
from arclight.core.errors import DomainError
from arclight.domain.audio_voices import AudioVoiceCreate
from arclight.persistence.audio_voices import AudioVoiceVersion
from arclight.persistence.models import Project, ProjectMember, VisualAsset


@pytest.fixture
def voice_case(client, settings):
    actor = account(client)
    configure(client, settings, "speech", "openai_tts")
    return actor, UUID(project(client))


def voice_payload(**changes):
    return AudioVoiceCreate.model_validate(
        {
            "creation_key": str(uuid4()),
            "speaker": "林川",
            "expected_version_no": 0,
            "parameters": {"voice": "test-voice", "speed": 1.1},
            "output_spec": {"format": "wav", "channels": 1},
            **changes,
        }
    )


def save_voice(client, actor, project_id, payload):
    with client.app.state.session_factory.begin() as db:
        row, created = create_voice_version(db, project_id, actor, payload)
        return read_voice(row), created


def test_voice_versions_freeze_the_exact_service_and_never_expose_credential(
    client, settings, voice_case
):
    actor, project_id = voice_case
    payload = voice_payload()
    first, created = save_voice(client, actor, project_id, payload)
    assert created and first.version_no == 1
    assert first.provider == "openai_tts" and first.model == "native-model"
    public = first.model_dump(mode="json")
    assert "credential" not in json.dumps(public).lower()
    assert "first-key" not in json.dumps(public)
    configure(client, settings, "speech", "siliconflow_tts", revision=1, key="replacement-key")
    replay, created = save_voice(
        client, actor, project_id, payload.model_copy(update={"expected_version_no": 999})
    )
    assert not created and replay == first
    second, created = save_voice(client, actor, project_id, voice_payload(expected_version_no=1))
    assert created and second.version_no == 2 and second.provider == "siliconflow_tts"
    assert second.model_config_version != first.model_config_version
    with client.app.state.session_factory() as db:
        old = require_voice(db, project_id, first.id, actor)
        assert read_voice(old) == first
        assert old.service_snapshot_json["configuration"]["protocol"] == "openai_tts"
        assert "credential_id" in old.service_snapshot_json
        assert "replacement-key" not in json.dumps(old.service_snapshot_json)


def test_voice_creation_rejects_changed_parameters_speaker_actor_and_stale_cas(client, voice_case):
    actor, project_id = voice_case
    payload = voice_payload()
    save_voice(client, actor, project_id, payload)
    for changes in [
        {"speaker": "旁白"},
        {"parameters": {"voice": "another-voice"}},
        {"output_spec": {"format": "mp3"}},
    ]:
        with pytest.raises(DomainError) as error:
            save_voice(client, actor, project_id, payload.model_copy(update=changes))
        assert error.value.code == "IDEMPOTENCY_CONFLICT"
    with pytest.raises(DomainError) as error:
        save_voice(client, actor, project_id, voice_payload())
    assert error.value.code == "AUDIO_VOICE_CONFLICT"
    member = account(client)
    with client.app.state.session_factory.begin() as db:
        db.add(ProjectMember(project_id=project_id, user_id=UUID(member), role="editor"))
    with pytest.raises(DomainError) as error:
        save_voice(client, member, project_id, payload)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize(
    "protocol,parameters,output",
    [
        ("qwen_tts", {"voice": "Cherry", "language_type": "Chinese"}, {"format": "wav"}),
        (
            "siliconflow_tts",
            {"voice": "model:anna", "speed": 0.25},
            {"format": "mp3", "sample_rate": 24000},
        ),
        (
            "openai_tts",
            {"voice": "alloy", "speed": 4, "instructions": "用平静而坚定的语气"},
            {"format": "flac"},
        ),
    ],
)
def test_voice_configuration_uses_native_supported_parameters(
    client, settings, protocol, parameters, output
):
    actor = account(client)
    configure(client, settings, "speech", protocol)
    project_id = UUID(project(client))
    saved, created = save_voice(
        client, actor, project_id, voice_payload(parameters=parameters, output_spec=output)
    )
    assert created and saved.parameters == parameters and saved.output_spec == output


@pytest.mark.parametrize(
    "protocol,parameters,output,code",
    [
        ("openai_tts", {}, {"format": "wav"}, "PROVIDER_VOICE_REQUIRED"),
        ("openai_tts", {"voice": "  "}, {}, "PROVIDER_VOICE_REQUIRED"),
        ("openai_tts", {"voice": "v", "speed": True}, {}, "PROVIDER_PARAMETERS_UNSUPPORTED"),
        ("openai_tts", {"voice": "v", "speed": 0.24}, {}, "PROVIDER_PARAMETERS_UNSUPPORTED"),
        ("openai_tts", {"voice": "v", "speed": 4.01}, {}, "PROVIDER_PARAMETERS_UNSUPPORTED"),
        ("openai_tts", {"voice": "v", "pitch": 1}, {}, "PROVIDER_PARAMETERS_UNSUPPORTED"),
        ("openai_tts", {"voice": "v", "emotion": "joy"}, {}, "PROVIDER_PARAMETERS_UNSUPPORTED"),
        ("openai_tts", {"voice": "v", "instructions": ""}, {}, "PROVIDER_PARAMETERS_UNSUPPORTED"),
        ("openai_tts", {"voice": "v"}, {"channels": 2}, "PROVIDER_PARAMETERS_UNSUPPORTED"),
        ("openai_tts", {"voice": "v"}, {"sample_rate": 24000}, "PROVIDER_PARAMETERS_UNSUPPORTED"),
        ("qwen_tts", {"voice": "v", "speed": 1}, {}, "PROVIDER_PARAMETERS_UNSUPPORTED"),
        ("qwen_tts", {"voice": "v"}, {"format": "mp3"}, "PROVIDER_PARAMETERS_UNSUPPORTED"),
        (
            "siliconflow_tts",
            {"voice": "v", "instructions": "开心"},
            {},
            "PROVIDER_PARAMETERS_UNSUPPORTED",
        ),
        (
            "siliconflow_tts",
            {"voice": "v"},
            {"sample_rate": 22050},
            "PROVIDER_PARAMETERS_UNSUPPORTED",
        ),
    ],
)
def test_invalid_voice_configuration_never_persists(
    client, settings, protocol, parameters, output, code
):
    actor = account(client)
    configure(client, settings, "speech", protocol)
    project_id = UUID(project(client))
    with pytest.raises(DomainError) as error:
        save_voice(
            client, actor, project_id, voice_payload(parameters=parameters, output_spec=output)
        )
    assert error.value.code == code
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AudioVoiceVersion)) == 0


def test_line_performance_can_change_delivery_without_changing_frozen_voice(client, voice_case):
    actor, project_id = voice_case
    saved, _ = save_voice(client, actor, project_id, voice_payload())
    with client.app.state.session_factory() as db:
        row = require_voice(db, project_id, saved.id, actor)
        spec = voice_spec(
            row, "请等我一下。", {"speed": 0.8, "instructions": "压低声音，焦急但克制"}
        )
        assert spec.prompt == "请等我一下。" and spec.prompt_version == "audio-line/1"
        assert spec.parameters == {
            "voice": "test-voice",
            "speed": 0.8,
            "instructions": "压低声音，焦急但克制",
        }
        assert spec.context == {} and read_voice(row) == saved
        for override in [{"voice": "another"}, {"language_type": "English"}, {"emotion": "joy"}]:
            with pytest.raises(DomainError) as error:
                voice_spec(row, "原句", override)
            assert error.value.code == "AUDIO_PERFORMANCE_UNSUPPORTED"
        with pytest.raises(DomainError) as error:
            voice_spec(row, "原句", {"speed": 5})
        assert error.value.code == "PROVIDER_PARAMETERS_UNSUPPORTED"


def test_voice_scopes_permissions_current_membership_and_project_lifecycle(client, voice_case):
    actor, project_id = voice_case
    payload = voice_payload(speaker="旁白")
    saved, _ = save_voice(client, actor, project_id, payload)
    member = account(client)
    with pytest.raises(DomainError) as error, client.app.state.session_factory() as db:
        require_voice(db, project_id, saved.id, member)
    assert error.value.status_code == 404
    with client.app.state.session_factory.begin() as db:
        membership = ProjectMember(project_id=project_id, user_id=UUID(member), role="viewer")
        db.add(membership)
        db.flush()
        membership_id = membership.id
    with client.app.state.session_factory() as db:
        assert read_voice(require_voice(db, project_id, saved.id, member)) == saved
    with pytest.raises(DomainError) as error:
        save_voice(client, member, project_id, payload)
    assert error.value.status_code == 403
    other_project = UUID(project(client))
    with pytest.raises(DomainError) as error, client.app.state.session_factory() as db:
        require_voice(db, other_project, saved.id, member)
    assert error.value.status_code == 404
    with client.app.state.session_factory.begin() as db:
        db.delete(db.get(ProjectMember, membership_id))
    with pytest.raises(DomainError) as error, client.app.state.session_factory() as db:
        require_voice(db, project_id, saved.id, member)
    assert error.value.status_code == 404
    with client.app.state.session_factory.begin() as db:
        db.get(Project, project_id).archived = True
    with pytest.raises(DomainError) as error:
        save_voice(client, actor, project_id, payload)
    assert error.value.code == "PROJECT_ARCHIVED"
    with client.app.state.session_factory() as db:
        assert read_voice(require_voice(db, project_id, saved.id, actor)) == saved


def test_voice_character_link_requires_usable_same_project_character(client, voice_case):
    actor, project_id = voice_case
    with client.app.state.session_factory.begin() as db:
        character = VisualAsset(project_id=project_id, asset_type="CHARACTER", name="林川")
        wrong_kind = VisualAsset(project_id=project_id, asset_type="SCENE", name="雨巷")
        db.add_all([character, wrong_kind])
        db.flush()
        character_id, wrong_id = character.id, wrong_kind.id
    saved, _ = save_voice(
        client, actor, project_id, voice_payload(character_asset_id=str(character_id))
    )
    assert saved.character_asset_id == character_id
    with pytest.raises(DomainError) as error:
        save_voice(
            client,
            actor,
            project_id,
            voice_payload(speaker="新角色", character_asset_id=str(wrong_id)),
        )
    assert error.value.code == "AUDIO_CHARACTER_INVALID"
    with client.app.state.session_factory.begin() as db:
        db.get(VisualAsset, character_id).status = "ARCHIVED"
    with pytest.raises(DomainError) as error:
        save_voice(
            client,
            actor,
            project_id,
            voice_payload(expected_version_no=1, character_asset_id=str(character_id)),
        )
    assert error.value.code == "ASSET_NOT_USABLE"
    other_project = UUID(project(client))
    with pytest.raises(DomainError) as error:
        save_voice(
            client, actor, other_project, voice_payload(character_asset_id=str(character_id))
        )
    assert error.value.status_code == 404


def test_voice_without_configured_speech_service_is_not_saved(client):
    actor, project_id = account(client), UUID(project(client))
    with pytest.raises(DomainError) as error:
        save_voice(client, actor, project_id, voice_payload())
    assert error.value.code == "SERVICE_INCOMPLETE"


@pytest.mark.parametrize("speaker", ["", " ", "\n\t"])
def test_voice_speaker_cannot_be_empty_or_whitespace(speaker):
    with pytest.raises(ValidationError):
        voice_payload(speaker=speaker)


def test_voice_history_rejects_update_delete_and_replace(client, voice_case):
    actor, project_id = voice_case
    saved, _ = save_voice(client, actor, project_id, voice_payload())
    with client.app.state.session_factory() as db:
        postgres = db.bind.dialect.name == "postgresql"
        original = db.get(AudioVoiceVersion, saved.id)
        assert original is not None
        frozen_configuration = original.configuration_json
        frozen_service_snapshot = original.service_snapshot_json
    statements = [
        "UPDATE audio_voice_versions SET updated_at=created_at WHERE id=:id",
        "DELETE FROM audio_voice_versions WHERE id=:id",
        "TRUNCATE TABLE audio_voice_versions CASCADE"
        if postgres
        else (
            "INSERT OR REPLACE INTO audio_voice_versions "
            "SELECT * FROM audio_voice_versions WHERE id=:id"
        ),
    ]
    for statement in statements:
        with (
            pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
            client.app.state.session_factory.begin() as db,
        ):
            db.execute(text(statement), {"id": saved.id if postgres else saved.id.hex})
        with client.app.state.session_factory.begin() as db:
            unchanged = db.get(AudioVoiceVersion, saved.id)
            assert unchanged is not None
            assert read_voice(unchanged) == saved
            assert unchanged.configuration_json == frozen_configuration
            assert unchanged.service_snapshot_json == frozen_service_snapshot


def test_postgres_same_voice_creation_is_serialized_and_new_version_cas_conflicts(
    client, voice_case
):
    if client.app.state.session_factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL voice version row locking required")
    actor, project_id = voice_case
    payload = voice_payload()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: save_voice(client, actor, project_id, payload), range(2)))
    assert sorted(created for _, created in results) == [False, True]
    assert results[0][0] == results[1][0]

    def append(_):
        try:
            return save_voice(client, actor, project_id, voice_payload(expected_version_no=1))[0]
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(append, range(2)))
    assert sum(result == "AUDIO_VOICE_CONFLICT" for result in results) == 1
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AudioVoiceVersion)) == 2
