"""Only installed adapters may advertise a media capability profile."""

from dataclasses import dataclass
from typing import Literal

from arclight.core.errors import DomainError
from arclight.core.image_service import OPENAI_IMAGE_MODELS
from arclight.domain.dependencies import IMAGE_KINDS
from arclight.domain.media_capabilities import Dimensions, MediaCapabilities, NumericParameterRange

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
    from arclight.providers.native_media_protocols import KINDS, capabilities

    if provider in KINDS:
        return capabilities(provider, model)
    if provider == "openai" and model in OPENAI_IMAGE_MODELS:
        return OPENAI_IMAGE_CAPABILITIES
    # Mock models are symbolic; real adapters must resolve their own model-specific profile.
    if provider != "mock":
        raise DomainError("PROVIDER_NOT_INSTALLED", "Media provider is not installed", 422)
    return MOCK_CAPABILITIES


def resolve_parameters(provider: str, model: str) -> frozenset[str]:
    """The installed adapter declares its options; a request cannot advertise support."""
    resolve_capabilities(provider, model)
    from arclight.providers.native_media_protocols import PARAMETERS

    if provider in PARAMETERS:
        parameters = PARAMETERS[provider]
        # OpenAI explicitly excludes delivery instructions for these two models.
        # Do not infer restrictions for unknown compatible models from their names.
        if provider == "openai_tts" and model in {"tts-1", "tts-1-hd"}:
            return parameters - {"instructions"}
        return parameters
    if provider == "openai":
        return frozenset({"quality"})
    return frozenset({"steps"})


CredentialSource = Literal["NONE", "SERVER", "ACCOUNT_SNAPSHOT"]


@dataclass(frozen=True)
class MediaAdapter:
    credential_source: CredentialSource
    simulation: bool = False


BUILTIN_ADAPTERS = {"mock": MediaAdapter("NONE", simulation=True), "openai": MediaAdapter("SERVER")}
ACCOUNT_ADAPTER = MediaAdapter("ACCOUNT_SNAPSHOT")


def adapter_contract(provider: str) -> MediaAdapter | None:
    from arclight.providers.native_media_protocols import KINDS

    return ACCOUNT_ADAPTER if provider in KINDS else BUILTIN_ADAPTERS.get(provider)


def credential_source(provider: str) -> CredentialSource | None:
    """Connection ownership is part of the registered adapter contract."""
    adapter = adapter_contract(provider)
    return adapter.credential_source if adapter else None


def parameter_ranges(provider: str, model: str) -> dict[str, NumericParameterRange]:
    from arclight.providers.native_media_protocols import NUMERIC_PARAMETER_RANGES

    supported = resolve_parameters(provider, model)
    return {
        name: limits.copy()
        for name, limits in NUMERIC_PARAMETER_RANGES.get(provider, {}).items()
        if name in supported
    }


def reference_count(inputs: list[dict]) -> int:
    return len(
        {item["source_asset_version_id"] for item in inputs if item["asset_type"] in IMAGE_KINDS}
    )
