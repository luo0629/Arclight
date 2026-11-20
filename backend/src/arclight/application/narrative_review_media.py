"""Read existing, exact media receipts without pretending to watch or listen."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import read_storyboard_content
from arclight.application.audio_production import (
    manifest_matches_line,
    require_live_line_inputs,
    validate_audio_line,
)
from arclight.application.audio_voice_profiles import (
    approved_voice_ledger,
    get_profile,
    manifest_voice,
)
from arclight.application.dependencies import exact_version
from arclight.application.narrative_review_evidence import evidence, invalid
from arclight.application.video_review import latest_video_review
from arclight.application.video_review_routing import current_temporal_review
from arclight.domain.narrative_review import (
    NarrativeEvidence,
    NarrativeLocation,
    NarrativeReviewTarget,
)
from arclight.domain.versions import digest
from arclight.persistence.audio_production import AudioLineBinding, AudioSnapshot
from arclight.persistence.models import DependencyManifest, Shot, StoredFile, Storyboard


def capture_media(
    db: Session,
    project_id: UUID,
    actor: str,
    target: NarrativeReviewTarget,
    locations: dict[int, NarrativeLocation],
) -> tuple[list[NarrativeEvidence], list[str]]:
    items = []
    gaps = []
    if target.storyboard_version_id is None:
        if target.video_candidate_ids:
            invalid("审查视频必须明确选择其精确分镜")
        gaps.append(f"剧本 {target.script_version_id} 未提供分镜及实际视频，不能确认镜头表演。")
    else:
        board = db.get(Storyboard, target.storyboard_version_id, populate_existing=True)
        if (
            board is None
            or board.script_version_id != target.script_version_id
            or board.status not in {"DRAFT", "READY_FOR_REVIEW", "APPROVED", "LOCKED"}
        ):
            invalid("分镜不属于精确送审剧本或已经失效")
        assert board is not None
        version, _ = exact_version(db, project_id, board.id)
        if version.stale:
            invalid("分镜版本已经失效")
        read_storyboard_content(board.content_json)
        shots = list(
            db.scalars(select(Shot).where(Shot.storyboard_id == board.id).order_by(Shot.shot_no))
        )
        for shot in shots:
            location = locations.get(shot.content_json["scene_no"])
            if location is None:
                continue
            if shot.content_json.get("scene_id") not in {None, str(location.scene_id)}:
                invalid("分镜镜头与剧本场景身份不符")
            items.append(
                evidence(
                    project_id,
                    "STORYBOARD",
                    location.model_copy(update={"shot_id": shot.id}),
                    {
                        "storyboard_id": str(board.id),
                        "storyboard_sha256": digest(board.content_json),
                        "storyboard_status": board.status,
                        "shot": shot.content_json,
                        "shot_status": shot.status,
                        "registry_revision": version.revision,
                    },
                )
            )
        if not target.video_candidate_ids:
            gaps.append(f"分镜 {board.id} 仅提供表演意图，没有实际候选视频观察。")
    for identity in target.video_candidate_ids:
        latest = latest_video_review(db, project_id, identity)
        record, inputs, _ = current_temporal_review(db, project_id, latest.report.report_id)
        location = locations.get(inputs.shot.content_json["scene_no"])
        if inputs.target.storyboard_id != target.storyboard_version_id or location is None:
            invalid("视频不属于送审精确分镜或场景")
        items.append(
            evidence(
                project_id,
                "VIDEO",
                location.model_copy(update={"shot_id": inputs.shot.id, "candidate_id": identity}),
                {
                    "record_sha256": digest(record.model_dump(mode="json")),
                    "report": record.report.model_dump(mode="json"),
                },
            )
        )
        gaps.extend(record.report.limitations)
        if record.report.conclusion != "PASS":
            gaps.append(
                f"候选 {identity} 的时序审查结论为 {record.report.conclusion}，须人工处理。"
            )
    expected_shots = {
        item.location.shot_id for item in items if item.kind == "STORYBOARD" and item.location
    }
    observed_shots = {
        item.location.shot_id for item in items if item.kind == "VIDEO" and item.location
    }
    if expected_shots - observed_shots:
        gaps.append(
            f"尚有 {len(expected_shots - observed_shots)} 个送审范围镜头未提供实际视频时序观察。"
        )
    scene_lines = {}
    # The screenplay is already validated by capture_review; use exact scene/line identities.
    from arclight.persistence.models import Script

    script = db.get(Script, target.script_version_id)
    assert script is not None
    for scene in script.content_json["scenes"]:
        if scene["scene_no"] in locations:
            for line in scene["dialogue"]:
                scene_lines[line["line_id"]] = (locations[scene["scene_no"]], line)
    for identity in target.audio_version_ids:
        version, asset = exact_version(db, project_id, identity)
        binding = db.scalar(
            select(AudioLineBinding).where(AudioLineBinding.asset_version_id == identity)
        )
        snapshot = db.get(AudioSnapshot, binding.snapshot_id) if binding else None
        if (
            asset.asset_type != "AUDIO"
            or version.stale
            or version.status not in {"DRAFT", "READY_FOR_REVIEW", "APPROVED", "LOCKED"}
            or binding is None
            or snapshot is None
            or snapshot.episode_id != next(iter(locations.values())).episode_id
            or binding.line_id not in scene_lines
        ):
            invalid("音频没有送审场景台词的有效精确绑定")
        assert binding is not None and snapshot is not None
        manifest = db.get(DependencyManifest, binding.dependency_manifest_id)
        if (
            manifest is None
            or manifest.project_id != project_id
            or manifest.target_asset_id != asset.id
            or version.metadata_json.get("dependency_manifest_id") != str(manifest.id)
            or not manifest_matches_line(manifest, snapshot, binding.line_id)
        ):
            invalid("音频输入清单与台词绑定不一致")
        assert manifest is not None
        require_live_line_inputs(db, project_id, manifest)
        validate_audio_line(db, identity, target.script_version_id, binding.line_id)
        file = db.get(StoredFile, binding.file_id, populate_existing=True)
        if (
            file is None
            or file.project_id != project_id
            or file.asset_version_id != identity
            or file.checksum != version.checksum
            or not file.rights_confirmed
        ):
            invalid("音频文件来源或授权凭证不一致")
        location, line = scene_lines[binding.line_id]
        selection = manifest_voice(manifest.snapshot_json["context"])
        profile = None
        if selection is not None:
            profile = get_profile(
                db, project_id, selection.character_asset_id, selection.profile_version_id
            )
            if (
                profile is None
                or profile.plan.ledger_version_id != selection.ledger_version_id
                or profile.plan.voice_at(selection.story_time) != selection.voice_version_id
            ):
                invalid("音频的剧情变声与精确声音档案不一致")
            _, approval_id = approved_voice_ledger(db, project_id, selection.ledger_version_id)
            if (
                approval_id != selection.ledger_approval_id
                or profile.ledger_approval_id != approval_id
            ):
                invalid("声音档案的账本批准来源不一致")
        items.append(
            evidence(
                project_id,
                "AUDIO",
                location.model_copy(update={"line_id": UUID(binding.line_id)}),
                {
                    "asset_version_id": str(identity),
                    "checksum": version.checksum,
                    "metadata_sha256": digest(version.metadata_json),
                    "status": version.status,
                    "line": line,
                    "duration_ms": binding.duration_ms,
                    "manifest_id": str(manifest.id),
                    "manifest_sha256": digest(manifest.snapshot_json),
                    "voice_selection": selection.model_dump(mode="json") if selection else None,
                    "voice_profile": profile.model_dump(mode="json") if profile else None,
                    "observation": "仅有句级来源与声音配置，未试听或声学评估",
                },
            )
        )
    audio_lines = {
        str(item.location.line_id)
        for item in items
        if item.kind == "AUDIO" and item.location is not None
    }
    if missing_lines := scene_lines.keys() - audio_lines:
        gaps.append(f"尚有 {len(missing_lines)} 句送审台词未提供精确音频来源，不能确认声音表演。")
    if audio_lines:
        gaps.append("本次音频仅核对精确来源与允许的剧情变声，未试听或作声学评估，须人工逐句核对。")
    return items, gaps
