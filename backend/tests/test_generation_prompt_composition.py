"""GEN-10: exact visual inputs produce a frozen, reproducible image prompt."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy import select
from test_asset_selection import approved
from test_asset_versions import private_objects  # noqa: F401
from test_assets import create, path
from test_generation_jobs import capture, payload

from arclight.application import generation_jobs
from arclight.application.generation_attempts import start_attempt
from arclight.domain.generation_prompts import BLOCK_LABELS, PROMPT_BLOCK_ORDER
from arclight.domain.versions import digest
from arclight.persistence.models import DependencyManifest, GenerationAttempt, VersionBinding


def _profile(client, asset, profile_name, values):
    response = client.put(
        f"{path(asset['project_id'])}/{asset['id']}/{profile_name}-profile",
        json={"profile": values, "expected_revision": asset["revision"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _visual_version(client, project_id):
    response = client.post(
        f"/api/v1/projects/{project_id}/visual-bible/versions",
        json={
            "expected_version_no": 0,
            "creation_key": str(uuid4()),
            "fields": {
                "art_style": "水墨分镜",
                "line_style": "干净细线",
                "palette": "靛蓝与暖黄",
                "prohibited": "现代广告牌",
                "negative_prompt": "文字、水印",
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _source_input(version):
    return {
        "source_asset_version_id": version["id"],
        "dependency_type": "STYLE_DEPENDENCY",
        "selectors": [],
    }


def _manifest(client, job):
    with client.app.state.session_factory() as db:
        return deepcopy(db.get(DependencyManifest, UUID(job["input_manifest_id"])).snapshot_json)


def test_prompt_composition_freezes_exact_sources_and_attempt_binding(client, episode):
    project_id = episode["project_id"]
    character = create(client, project_id, "CHARACTER", "林澈")
    character = _profile(
        client,
        character,
        "character",
        {
            "fixed_features": ["左眉淡疤"],
            "variable_features": ["湿发"],
            "outfits": [{"id": str(uuid4()), "name": "夜行衣", "description": "深色短外套"}],
            "default_outfit_id": None,
            "expressions": ["警觉"],
        },
    )
    character_version = approved(client, character)

    scene = create(client, project_id, "SCENE", "旧车站")
    scene = _profile(
        client,
        scene,
        "scene",
        {
            "layout": "站台向南延伸",
            "time_of_day": "午夜",
            "lighting": "冷白顶灯",
            "palette": "蓝灰",
            "environment": "雨水从屋檐落下",
            "reference": None,
        },
    )
    scene_version = approved(client, scene)

    prop = create(client, project_id, "PROP", "铜钥匙")
    prop = _profile(
        client,
        prop,
        "prop",
        {
            "appearance": "边缘磨损，缺一齿",
            "holder": None,
            "story_state": "刚从信封中取出",
        },
    )
    prop_version = approved(client, prop)

    style_response = client.post(
        path(project_id),
        json={
            "asset_type": "STYLE",
            "name": "手绘水彩",
            "metadata": {"description": "纸面颗粒与湿画边缘"},
        },
    )
    assert style_response.status_code == 201, style_response.text
    style_version = approved(client, style_response.json())
    visual_version = _visual_version(client, project_id)
    inputs = [
        _source_input(character_version),
        _source_input(scene_version),
        _source_input(prop_version),
        _source_input(style_version),
    ]
    body = payload(
        inputs=inputs,
        spec={
            **payload()["spec"],
            "prompt": "主角在雨中转身",
            "negative_prompt": "低清晰度",
            "context": {
                "shot_description": "视线落在钥匙上",
                "camera_language": "中景，侧后方视角",
                "continuity_context": "钥匙始终在右手",
            },
        },
    )
    first_key = str(uuid4())
    response = capture(client, project_id, body, key=first_key)
    assert response.status_code == 201, response.text
    job = response.json()
    frozen = _manifest(client, job)
    composition = frozen["context"]["prompt_composition"]
    spec = frozen["context"]["generation"]
    blocks = {block["key"]: block for block in composition["blocks"]}

    assert composition["composer_version"] == "1"
    assert [block["key"] for block in composition["blocks"]] == list(PROMPT_BLOCK_ORDER)
    assert [source["asset_type"] for source in composition["sources"][1:]] == [
        "STYLE",
        "CHARACTER",
        "SCENE",
        "PROP",
    ]
    assert composition["sources"][0]["version_id"] == visual_version["id"]
    assert "水墨分镜" in blocks["project_style"]["content"]
    assert "纸面颗粒与湿画边缘" in blocks["project_style"]["content"]
    assert "左眉淡疤" in blocks["character_constraints"]["content"]
    assert "空间布局：站台向南延伸" in blocks["location_constraints"]["content"]
    assert "剧情状态：刚从信封中取出" in blocks["prop_constraints"]["content"]
    assert "视线落在钥匙上" in blocks["shot_description"]["content"]
    assert "侧后方视角" in blocks["camera_language"]["content"]
    assert "钥匙始终在右手" in blocks["continuity_context"]["content"]
    rendered = [
        f"【{BLOCK_LABELS[key]}】\n{blocks[key]['content']}"
        for key in PROMPT_BLOCK_ORDER
        if blocks[key]["content"] and key != "negative_constraints"
    ]
    assert spec["prompt"] == "\n\n".join(rendered)
    assert spec["negative_prompt"] == "现代广告牌\n文字、水印\n低清晰度"
    assert blocks["negative_constraints"]["content"] == spec["negative_prompt"]
    assert blocks["negative_constraints"]["source_keys"] == [
        f"{composition['sources'][0]['source_key']}.fields.prohibited",
        f"{composition['sources'][0]['source_key']}.fields.negative_prompt",
        "request.spec.negative_prompt",
    ]
    assert {source["asset_version_id"] for source in composition["sources"][1:]} == {
        character_version["id"],
        scene_version["id"],
        prop_version["id"],
        style_version["id"],
    }
    assert composition["provider_parameters"]["parameters"] == spec["parameters"]
    assert "steps" not in spec["prompt"]
    assert composition["prompt_sha256"] == digest(spec["prompt"])
    assert composition["negative_prompt_sha256"] == digest(spec["negative_prompt"])

    reverse_response = capture(
        client,
        project_id,
        payload(inputs=list(reversed(inputs)), spec=body["spec"]),
        key=str(uuid4()),
    )
    assert reverse_response.status_code == 201, reverse_response.text
    reverse_job = reverse_response.json()
    assert _manifest(client, reverse_job)["context"]["prompt_composition"] == composition

    current_character = client.get(path(project_id) + "/" + character["id"]).json()
    updated = _profile(
        client,
        current_character,
        "character",
        {
            "fixed_features": ["新版特征"],
            "variable_features": [],
            "outfits": [],
            "default_outfit_id": None,
            "expressions": [],
        },
    )
    assert updated["character_profile"]["fixed_features"] == ["新版特征"]
    changed_visual = client.post(
        f"/api/v1/projects/{project_id}/visual-bible/versions",
        json={
            "expected_version_no": 1,
            "creation_key": str(uuid4()),
            "fields": {"art_style": "新版风格"},
        },
    )
    assert changed_visual.status_code == 201, changed_visual.text
    replay = capture(client, project_id, body, key=first_key)
    assert replay.status_code == 201, replay.text
    assert replay.json()["input_manifest_id"] == job["input_manifest_id"]
    assert _manifest(client, job) == frozen

    with client.app.state.session_factory.begin() as db:
        attempt = start_attempt(db, UUID(job["id"]), job["revision"])
        attempt_id = attempt.id
    with client.app.state.session_factory() as db:
        saved_attempt = db.get(GenerationAttempt, attempt_id)
        assert saved_attempt.input_snapshot_json == frozen
        binding = db.scalar(
            select(VersionBinding).where(
                VersionBinding.resource_type == "GENERATION",
                VersionBinding.resource_id == UUID(job["id"]),
            )
        )
        assert binding.snapshot_json["prompt_composition_sha256"] == digest(composition)
        assert any(
            item["name"] == "media-prompt-composer" and item["version"] == "1"
            for item in binding.snapshot_json["components"]
        )


def test_missing_exact_profile_snapshot_is_rejected_atomically(client, episode, monkeypatch):
    project_id = episode["project_id"]
    character = create(client, project_id, "CHARACTER")
    version = approved(client, character)
    original = generation_jobs.exact_version

    def without_snapshot(db, project, version_id):
        row, asset = original(db, project, version_id)
        return (
            SimpleNamespace(
                id=row.id,
                metadata_json={},
                stale=row.stale,
                status=row.status,
                version_no=row.version_no,
                checksum=row.checksum,
            ),
            asset,
        )

    monkeypatch.setattr(generation_jobs, "exact_version", without_snapshot)
    response = capture(client, project_id, payload(inputs=[_source_input(version)]))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PROMPT_SOURCE_SNAPSHOT_MISSING"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(GenerationAttempt.id)) is None
        assert db.scalar(select(DependencyManifest.id)) is None


def test_multiple_versions_of_one_visual_asset_are_rejected(client, episode):
    project_id = episode["project_id"]
    character = create(client, project_id, "CHARACTER")
    first = approved(client, character)
    current = client.get(path(project_id) + "/" + character["id"]).json()
    second = approved(client, current)
    response = capture(
        client,
        project_id,
        payload(inputs=[_source_input(first), _source_input(second)]),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PROMPT_SOURCE_VERSION_CONFLICT"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(DependencyManifest.id)) is None
