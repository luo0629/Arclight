"""Only installed adapters may advertise a media capability profile."""

from arclight.core.errors import DomainError
from arclight.core.image_service import OPENAI_IMAGE_MODELS
from arclight.domain.dependencies import IMAGE_KINDS
from arclight.domain.media_capabilities import Dimensions, MediaCapabilities

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

OPENAI_IMAGE_CAPABILITIES = MediaCapabilities(
    provider="openai",
    version="openai-images:1",
    task_types=frozenset({"IMAGE"}),
    max_reference_images=0,
    supports_seed=False,
    max_width=1536,
    max_height=1536,
    max_pixels=2359296,
    sizes=(
        Dimensions(width=1024, height=1024),
        Dimensions(width=1536, height=1024),
        Dimensions(width=1024, height=1536),
    ),
    max_batch_size=4,
    supports_query=False,
    supports_cancel=False,
    supports_idempotency=False,
    execution_mode="SYNC",
)


def resolve_capabilities(provider: str, model: str) -> MediaCapabilities:
    if provider == "openai" and model in OPENAI_IMAGE_MODELS:
        return OPENAI_IMAGE_CAPABILITIES
    # Mock models are symbolic; real adapters must resolve their own model-specific profile.
    if provider != "mock":
        raise DomainError("PROVIDER_NOT_INSTALLED", "Media provider is not installed", 422)
    return MOCK_CAPABILITIES


def resolve_parameters(provider: str, model: str) -> frozenset[str]:
    """The installed adapter declares its options; a request cannot advertise support."""
    resolve_capabilities(provider, model)
    if provider == "openai":
        return frozenset({"quality"})
    return frozenset({"steps"})


def reference_count(inputs: list[dict]) -> int:
    return len(
        {item["source_asset_version_id"] for item in inputs if item["asset_type"] in IMAGE_KINDS}
    )
