"""Cancellation requests are best effort; only confirmed cancellation is terminal."""

from typing import Literal, Protocol

from arclight.domain.generation_requests import MediaInput
from arclight.domain.media_capabilities import MediaCapabilities
from arclight.providers.media_capabilities import MOCK_CAPABILITIES
from arclight.providers.media_query import ProviderLookup


class CancellationReceipt(MediaInput):
    status: Literal[
        "CONFIRMED", "REQUESTED", "UNSUPPORTED", "NOT_FOUND", "FAILED", "RESULT_UNKNOWN"
    ]


class MediaCancelProvider(Protocol):
    capabilities: MediaCapabilities

    def cancel(self, lookup: ProviderLookup) -> CancellationReceipt: ...


class MockMediaCancelProvider:
    capabilities = MOCK_CAPABILITIES

    def cancel(self, lookup: ProviderLookup) -> CancellationReceipt:
        return CancellationReceipt(status="UNSUPPORTED")


def get_media_cancel_provider() -> MediaCancelProvider:
    return MockMediaCancelProvider()
