"""Only installed adapters may advertise a media capability profile."""

from arclight.core.errors import DomainError
from arclight.domain.dependencies import IMAGE_KINDS
from arclight.domain.media_capabilities import MediaCapabilities

MOCK_CAPABILITIES = MediaCapabilities(
    provider="mock",
    version="mock:1",
    task_types=frozenset({"IMAGE", "VIDEO", "AUDIO", "RENDER"}),
    max_reference_images=64,
    supports_seed=True,
    max_width=8192,
    max_height=8192,
    max_pixels=33554432,
    max_batch_size=8,
    supports_query=False,
    supports_cancel=False,
    supports_idempotency=True,
    execution_mode="SYNC",
    max_video_seconds=120,
)


def resolve_capabilities(provider: str, model: str) -> MediaCapabilities:
    # Mock models are symbolic; real adapters must resolve their own model-specific profile.
    if provider != "mock":
        raise DomainError("PROVIDER_NOT_INSTALLED", "Media provider is not installed", 422)
    return MOCK_CAPABILITIES


def reference_count(inputs: list[dict]) -> int:
    return len(
        {item["source_asset_version_id"] for item in inputs if item["asset_type"] in IMAGE_KINDS}
    )
