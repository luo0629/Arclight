"""HTTP model scenarios that consume actual art-tool feedback."""

import json
from copy import deepcopy
from uuid import UUID, uuid4, uuid5

import httpx
from test_agent_model import call_response
from test_agent_runtime import final, tool_feedback
from test_art_proposals import proposal_payload
from test_art_research import add_image, prepare_art
from test_assets import create as create_asset
from test_assets import path as asset_path

from arclight.application.agent_tools import build_agent_tool_registry, grant_agent_tools
from arclight.domain.agent_tools import AgentToolGrantRequest
from arclight.domain.art_direction import ArtDirectionTask


def reference_set(client, episode, *, count=2, name="雨夜角色"):
    asset = create_asset(client, episode["project_id"], name=name)
    versions = [add_image(client, asset, color=color) for color in ("blue", "red", "green")[:count]]
    if versions:
        images = [
            {
                "id": str(uuid4()),
                "label": f"角度{index}",
                "kind": "INITIAL" if index == 0 else "ANGLE",
                "angle": "" if index == 0 else "侧面",
                "source": {"asset_id": asset["id"], "asset_version_id": version["id"]},
            }
            for index, version in enumerate(versions)
        ]
        asset = client.get(asset_path(episode["project_id"]) + "/" + asset["id"]).json()
        response = client.put(
            asset_path(episode["project_id"]) + "/" + asset["id"] + "/character-profile",
            json={
                "expected_revision": asset["revision"],
                "profile": {
                    "fixed_features": ["银发", "圆眼镜"],
                    "variable_features": ["表情"],
                    "images": images,
                    "standard_image_id": images[0]["id"],
                },
            },
        )
        assert response.status_code == 200, response.text
        asset = response.json()
    return asset, versions


def prepare_direction(client, episode, settings, *, target_ids=(), minimum=2, source=None):
    run_id, actor, source = prepare_art(client, episode, settings, source)
    with client.app.state.session_factory.begin() as db:
        grant_agent_tools(
            db,
            AgentToolGrantRequest(
                run_id=run_id,
                agent_name="art_director",
                allowed_tools=["investigate_art", "compare_art_options"],
            ),
            actor,
            build_agent_tool_registry(settings=settings),
        )
    task = ArtDirectionTask(
        visual_goal="比较雨夜角色的轮廓与配色，提供制作提示词",
        constraints=["保持人物固定特征"],
        target_asset_ids=[UUID(value) for value in target_ids],
        minimum_reference_images=minimum,
    )
    return run_id, actor, source, task


def seed_from_payload(payload):
    for message in payload["messages"]:
        if message["role"] == "user" and isinstance(message.get("content"), str):
            value = json.loads(message["content"]).get("data", {})
            if value.get("schema_version") == "art-direction/1":
                return value
    raise AssertionError("Missing frozen art seed")


def draft_from_feedback(payload, *, existing=False, bible=False):
    seed = seed_from_payload(payload)
    namespace = UUID(seed["script_version_id"])

    def identity(label):
        return str(uuid5(namespace, label))

    materials = [
        material
        for feedback in tool_feedback(payload)
        if feedback["status"] == "SUCCEEDED" and feedback["tool_name"] == "investigate_art"
        for material in feedback["result"]["materials"]
    ]
    evidence = [
        {key: value for key, value in material.items() if key != "content"}
        for material in materials
    ]
    sources = [
        {
            "kind": "VERSION",
            "source_id": identity("script-source"),
            "resource_type": "SCRIPT",
            "resource_id": seed["script_version_id"],
            "content_sha256": seed["script_sha256"],
        }
    ]
    if seed["visual_bible_source"] is not None:
        sources.append(deepcopy(seed["visual_bible_source"]))
    references = []
    for material in materials:
        if material["source_kind"] != "ASSET":
            continue
        content = material["content"]
        source_id = identity("source-" + material["source_id"])
        sources.append(
            {
                "kind": "VERSION",
                "source_id": source_id,
                "resource_type": "ASSET_VERSION",
                "resource_id": material["source_id"],
                "content_sha256": content["checksum"],
            }
        )
        references.append(
            {
                "reference_id": identity("reference-" + material["source_id"]),
                "source_id": source_id,
                "asset": {
                    "asset_id": content["asset_id"],
                    "asset_version_id": material["source_id"],
                },
                "asset_type": content["asset"]["asset_type"],
                "usage": "APPROVED_REFERENCE",
                "purpose": "比较轮廓及配色",
            }
        )
    proposal = proposal_payload(project_id=seed["project_id"])
    proposal.update(
        episode_id=seed["episode_id"],
        origin="AGENT",
        visual_goal=seed["task"]["visual_goal"],
        constraints=seed["task"]["constraints"],
        sources=sources,
        references=references,
    )
    options = []
    for index in range(2):
        option = deepcopy(proposal["options"][0])
        option["option_id"] = identity(f"option-{index}")
        option["title"] = "蓝灰雨夜" if index == 0 else "暖红轮廓"
        design = option["designs"][0]
        design["design_id"] = identity(f"design-{index}")
        if existing:
            directory = next(m["content"] for m in materials if m["source_kind"] == "DIRECTORY")
            design.pop("profile", None)
            design.update(target=directory["baseline"], name="雨夜新提案")
        if bible:
            visual = next(m["content"] for m in materials if m["source_kind"] == "VISUAL")
            style = visual["global"]["style"]
            option["designs"] = [
                {
                    "kind": "VISUAL_BIBLE",
                    "design_id": design["design_id"],
                    "version_id": style["version_id"],
                    "version_no": style["version_no"],
                    "content_sha256": seed["visual_bible_source"]["content_sha256"],
                    "fields": {"art_style": "油画" if index == 0 else "版画"},
                }
            ]
        option["source_ids"] = [source["source_id"] for source in sources]
        option["reference_ids"] = [ref["reference_id"] for ref in references]
        option["prompts"][0].update(
            fragment_id=identity(f"prompt-{index}"),
            design_ids=[design["design_id"]],
            source_ids=option["source_ids"],
            reference_ids=option["reference_ids"],
            text=f"{option['title']}，保持银发和圆眼镜，保留纸张纹理。",
        )
        options.append(option)
    proposal["options"] = options
    proposal["preferred_option_id"] = options[0]["option_id"]
    return {
        "proposal": proposal,
        "evidence": evidence,
        "assessments": [
            {
                "option_id": option["option_id"],
                "strengths": "参考图的轮廓清晰，可保持人物辨识度",
                "tradeoffs": "背景需降低饱和度，避免遮蔽银发边缘",
                "evidence_manifest_ids": list(dict.fromkeys(m["manifest_id"] for m in materials)),
                "reference_ids": option["reference_ids"],
            }
            for option in options
        ],
    }


def install_direction_model(
    model_service,
    *,
    search="雨夜角色",
    fallback=None,
    human=False,
    existing=False,
    bible=False,
    mutate_draft=None,
    mutate_final=None,
    on_http=None,
    bypass_compare=False,
    include_images=True,
):
    sent, errors, drafts = [], [], []

    def respond(request):
        try:
            payload = json.loads(request.content)
            sent.append(payload)
            if on_http:
                on_http(payload)
            feedback = tool_feedback(payload)
            answered = any(
                message["role"] == "user" and "human_decision" in message.get("content", "")
                for message in payload["messages"]
            )
            investigations = [f for f in feedback if f["tool_name"] == "investigate_art"]
            comparisons = [f for f in feedback if f["tool_name"] == "compare_art_options"]
            if not feedback:
                seed = seed_from_payload(payload)
                directory_query = (
                    {"path": "directories", "asset_ids": seed["task"]["target_asset_ids"]}
                    if seed["task"]["target_asset_ids"]
                    else {"path": "catalog", "asset_type": "CHARACTER", "name": search}
                )
                value = call_response(
                    ("visual", "investigate_art", {"query": {"path": "visual"}}),
                    ("model", "investigate_art", {"query": {"path": "model"}}),
                    ("directory", "investigate_art", {"query": directory_query}),
                )
            elif human and not answered:
                value = final("HUMAN_REVIEW")
            elif not any(
                m["source_kind"] == "ASSET"
                for f in investigations
                if f["status"] == "SUCCEEDED"
                for m in f["result"]["materials"]
            ):
                directories = [
                    m["content"]
                    for f in investigations
                    if f["status"] == "SUCCEEDED"
                    for m in f["result"]["materials"]
                    if m["source_kind"] == "DIRECTORY"
                ]
                versions = list(
                    dict.fromkeys(
                        candidate["source"]["asset_version_id"]
                        for directory in directories
                        for candidate in directory["image_candidates"]
                    )
                )
                if not versions and fallback and len(investigations) == 3:
                    value = call_response(
                        (
                            "fallback",
                            "investigate_art",
                            {
                                "query": {
                                    "path": "catalog",
                                    "asset_type": "CHARACTER",
                                    "name": fallback,
                                }
                            },
                        )
                    )
                elif versions and len(investigations) <= 4:
                    value = call_response(
                        (
                            "images",
                            "investigate_art",
                            {
                                "query": {
                                    "path": "references",
                                    "version_ids": versions,
                                    "include_images": include_images,
                                }
                            },
                        )
                    )
                else:
                    value = final("HUMAN_REVIEW")
            elif not comparisons:
                draft = draft_from_feedback(payload, existing=existing, bible=bible)
                if mutate_draft:
                    mutate_draft(draft)
                drafts.append(draft)
                if bypass_compare:
                    value = final(result={"draft": draft, "comparison_sha256": "a" * 64})
                else:
                    value = call_response(("compare", "compare_art_options", {"draft": draft}))
            elif comparisons[-1]["status"] != "SUCCEEDED":
                value = final("HUMAN_REVIEW")
            else:
                result = {
                    "draft": deepcopy(drafts[-1]),
                    "comparison_sha256": comparisons[-1]["result"]["comparison"][
                        "comparison_sha256"
                    ],
                }
                if mutate_final:
                    mutate_final(result)
                value = final(result=result)
            return httpx.Response(200, json=value)
        except Exception as error:
            errors.append(repr(error))
            raise

    model_service(respond)
    return sent, errors, drafts
