"""ASSET-08 exact, immutable and explicitly declared generation history."""

from copy import deepcopy
from uuid import uuid4

import pytest
from test_asset_selection import approved, current, restore
from test_asset_versions import draft, private_objects, save, state, versions  # noqa: F401
from test_assets import create, rename


def generation(**values):
    return {
        "provider": "external-test",
        "model": "test-image-model",
        "prompt": "蓝色夜景",
        "negative_prompt": "低清晰度",
        "prompt_version": "painting-v2",
        "seed": 42,
        "parameters": {"steps": 20, "guidance": 7.5, "sampler": "test", "tiling": False},
        "references": [],
        **values,
    }


def test_manual_import_unknown_legacy_and_declared_history(client, episode):
    row = create(client, episode["project_id"])
    first = save(client, row)
    assert first["provenance"]["kind"] == "MANUAL_IMPORT"
    assert first["provenance"]["generation"] is None
    row = current(client, row)
    second = save(client, row, generation=generation())
    provenance = second["provenance"]
    assert provenance["kind"] == "EXTERNAL_DECLARATION"
    assert provenance["verification"] == "USER_DECLARED"
    assert provenance["width"] == 8 and provenance["height"] == 12
    assert provenance["generation"] == {k: v for k, v in generation().items() if k != "references"}
    snapshot = deepcopy(second)
    row = rename(client, current(client, row), "changed").json()
    restored = restore(client, row, second).json()
    assert restored["provenance"] == provenance
    assert client.get(versions(row) + "/" + second["id"]).json() == snapshot


def test_reference_provenance_is_exact_and_survives_later_changes(client, episode):
    reference_asset = create(client, episode["project_id"], "SCENE", "初始夜景")
    source = approved(client, reference_asset)
    target = create(client, episode["project_id"], "STYLE")
    ref = {"asset_id": reference_asset["id"], "asset_version_id": source["id"]}
    image = save(client, target, generation=generation(references=[ref]))
    saved = deepcopy(image["provenance"])
    assert saved["references"][0] == {
        **ref,
        "version_no": 1,
        "checksum": source["checksum"],
        "asset_name": "初始夜景",
        "asset_type": "SCENE",
    }
    rename(client, current(client, reference_asset), "新名")
    state(client, reference_asset, source, "STALE")
    assert client.get(versions(target) + "/" + image["id"]).json()["provenance"] == saved
    target = current(client, target)
    assert (
        client.post(
            versions(target), json=draft(target, generation=generation(references=[ref]))
        ).status_code
        == 422
    )


@pytest.mark.parametrize(
    "values",
    [
        {"seed": True},
        {"seed": -1},
        {"seed": 9007199254740992},
        {"model": ""},
        {"provider": ""},
        {"parameters": {"nested": {"secret": "not accepted"}}},
        {"parameters": {"x" * 81: 1}},
        {"prompt": "x" * 12001},
        {"width": 8},
    ],
)
def test_invalid_provenance_rejected_atomically(client, episode, values):
    row = create(client, episode["project_id"])
    response = client.post(versions(row), json=draft(row, generation=generation(**values)))
    assert response.status_code == 422, response.text
    assert client.get(versions(row)).json() == []
    assert current(client, row)["revision"] == 0


def test_reference_identity_duplicates_and_project_gate(client, episode):
    row = create(client, episode["project_id"])
    source = approved(client, row)
    ref = {"asset_id": row["id"], "asset_version_id": source["id"]}
    other_project = client.post("/api/v1/projects", json={"name": "other"}).json()
    target = create(client, other_project["id"])
    for refs in ([ref], [ref, ref], [{**ref, "asset_id": str(uuid4())}]):
        assert (
            client.post(
                versions(target), json=draft(target, generation=generation(references=refs))
            ).status_code
            == 422
        )
