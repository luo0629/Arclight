"""Prepare private, reproducible frame evidence for the shared Agent gateway."""

import base64
import json
from contextvars import ContextVar
from dataclasses import dataclass
from hashlib import sha256
from math import ceil
from uuid import UUID, uuid5

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.generation_video_inputs import load_video_images
from arclight.application.render_inputs import file_storage
from arclight.application.video_review_work import VideoReviewInputs
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelRequest, AgentUserMessage, ContextImage
from arclight.domain.file_policy import POLICIES
from arclight.domain.versions import digest
from arclight.domain.video_temporal_reviewer import TEMPORAL_AGENT, TemporalFrame
from arclight.media.execution import MediaExecutionError, execute_media
from arclight.media.video_frames import SAMPLE_COUNT
from arclight.persistence.models import DependencyManifest, Shot, StoredFile


@dataclass(frozen=True)
class VideoEvidence:
    samples: list[TemporalFrame]
    sheet: bytes
    references: tuple[ContextImage, ...]
    end_covered: bool
    shot_context: dict

    @property
    def sheet_sha256(self) -> str:
        return sha256(self.sheet).hexdigest()


def collect_evidence(db: Session, settings: Settings, inputs: VideoReviewInputs) -> VideoEvidence:
    video = inputs.check.video
    assert video is not None
    file = db.scalar(
        select(StoredFile).where(
            StoredFile.project_id == inputs.job.project_id,
            StoredFile.asset_version_id == inputs.version.id,
            StoredFile.object_key == inputs.version.object_key,
            StoredFile.checksum == inputs.target.checksum,
            StoredFile.media_type == video.media_type,
        )
    )
    if file is None or file.byte_size != video.byte_size:
        raise DomainError("ASSET_OBJECT_UNAVAILABLE", "视频对象与不可变候选不匹配", 409)
    data = file_storage(settings, file).read(
        file.object_key, POLICIES[video.media_type].max_bytes, file.checksum
    )
    if len(data) != video.byte_size:
        raise DomainError("ASSET_OBJECT_UNAVAILABLE", "实测视频大小与候选不匹配", 409)
    try:
        output = json.loads(execute_media("sample", video.media_type, data))
        samples = [TemporalFrame.model_validate(row) for row in output["samples"]]
        sheet = base64.b64decode(output["sheet"], validate=True)
        times = [frame.time_ms for frame in samples]
        if (
            output["decoded_frames"] != video.decoded_frames
            or not 2 <= len(samples) <= SAMPLE_COUNT
            or times != sorted(set(times))
            or times[-1] >= video.duration_ms
            or not sheet.startswith(b"\xff\xd8")
        ):
            raise ValueError("Sample output does not match checked media")
    except MediaExecutionError as error:
        if error.reason == "BUSY":
            raise DomainError("VIDEO_SAMPLER_BUSY", "视频取证暂时繁忙", 409) from error
        raise DomainError(
            "VIDEO_TEMPORAL_EVIDENCE_UNAVAILABLE", "视频未取得有效时序帧", 409
        ) from error
    except (ValidationError, ValueError, TypeError, KeyError) as error:
        raise DomainError(
            "VIDEO_TEMPORAL_EVIDENCE_UNAVAILABLE", "视频未取得有效时序帧", 409
        ) from error

    original = db.get(DependencyManifest, inputs.job.input_manifest_id)
    assert original is not None
    references = tuple(
        ContextImage(
            uuid5(inputs.target.candidate_id, f"first-frame:{sha256(ref.data).hexdigest()}"),
            ref.media_type,
            ref.data,
        )
        for ref in load_video_images(db, inputs.job, original.snapshot_json)
    )
    return VideoEvidence(
        samples,
        sheet,
        references,
        times[0] == 0 and times[-1] >= max(1, video.duration_ms - ceil(1000 / video.frame_rate)),
        approved_shot_context(db, inputs.shot),
    )


def approved_shot_context(db: Session, shot: Shot) -> dict:
    nearby = db.scalars(
        select(Shot)
        .where(
            Shot.storyboard_id == shot.storyboard_id,
            Shot.shot_no >= shot.shot_no - 1,
            Shot.shot_no <= shot.shot_no + 1,
        )
        .order_by(Shot.shot_no)
    )
    return {
        "selected_shot_id": str(shot.id),
        "storyboard_id": str(shot.storyboard_id),
        "dynamic_required": (shot.content_json.get("performance") or {}).get("motion_requirement")
        == "dynamic_required",
        "shot_plan": [
            {
                "shot_id": str(item.id),
                "shot_no": item.shot_no,
                "description": item.description,
                "duration_ms": item.duration_ms,
                "content": item.content_json,
            }
            for item in nearby
        ],
    }


# Frame pixels remain ephemeral. The persistent Agent seed contains their exact
# identities and checksums, so a restart must reproduce the same evidence.
TEMPORAL_IMAGES: ContextVar[tuple[UUID, str, dict[UUID, ContextImage]] | None] = ContextVar(
    "video_temporal_images", default=None
)


def temporal_gateway_evidence(request: AgentModelRequest) -> tuple[dict, dict[UUID, ContextImage]]:
    active = TEMPORAL_IMAGES.get()
    seed = request.messages[0]
    if (
        active is None
        or request.agent_name != "continuity_reviewer"
        or request.agent_version != TEMPORAL_AGENT
        or not isinstance(seed, AgentUserMessage)
        or active[0] != request.run_id
        or active[1] != digest(seed.data)
        or request.image_ids() != list(active[2])
    ):
        raise DomainError("CONTEXT_REFERENCE_INVALID", "视频模型请求与精确取证不匹配", 409)
    return {"temporal_evidence_sha256": active[1]}, active[2]
