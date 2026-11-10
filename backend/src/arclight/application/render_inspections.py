"""Compare isolated measurements with the exact immutable output profile."""

import hashlib
from typing import cast

from pydantic import ValidationError

from arclight.domain.render_inspections import (
    InspectionFailure,
    RenderInspectionResult,
    RenderMediaMeasurement,
    check_measurement,
)
from arclight.domain.render_manifests import RenderOutputProfile
from arclight.media.execution import MediaExecutionError, MediaObserver, execute_media
from arclight.media.render_plan import RENDER_TIMEOUT


def inspect_candidate(
    movie: bytes,
    profile: RenderOutputProfile,
    duration_ms: int,
    *,
    observer: MediaObserver,
) -> RenderInspectionResult:
    measurement, failure = None, None
    try:
        raw = execute_media("inspect", "render", movie, timeout=RENDER_TIMEOUT, observer=observer)
        measurement = RenderMediaMeasurement.model_validate_json(raw)
    except MediaExecutionError as exc:
        failure = (
            exc.reason
            if exc.reason
            in {
                "INVALID_MEDIA",
                "TIME_LIMIT",
                "MEMORY_LIMIT",
                "RESOURCE_LIMIT",
                "OUTPUT_LIMIT",
                "SANDBOX_UNAVAILABLE",
                "UNAVAILABLE",
                "BUSY",
            }
            else "FAILED"
        )
    except ValidationError:
        failure = "INVALID_MEDIA"
    return RenderInspectionResult(
        output_sha256=hashlib.sha256(movie).hexdigest(),
        byte_size=len(movie),
        measurement=measurement,
        checks=check_measurement(measurement, profile, duration_ms),
        failure=cast(InspectionFailure | None, failure),
    )
