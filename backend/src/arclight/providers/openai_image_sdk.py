"""One synchronous Images SDK call on the frozen account route."""

import httpx
from openai import OpenAI, OpenAIError

from arclight.core.errors import DomainError
from arclight.domain.model_gateway import GatewayFailure
from arclight.providers.media_io import JSON_RESPONSE_LIMIT, read_provider_response
from arclight.providers.sdk_logging import suppress_sdk_logging


class _ImageTransport(httpx.BaseTransport):
    def __init__(self, url: str, api_key: str, transport: httpx.BaseTransport):
        self.url = httpx.URL(url)
        self.api_key = api_key
        self.transport = transport
        self.sent = False

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self.sent:
            raise GatewayFailure("PROVIDER_UNAVAILABLE")
        if request.method != "POST" or request.url != self.url or request.url.query:
            raise GatewayFailure("INVALID_SERVICE_URL", not_accepted=True)
        # Only the frozen credential and native image headers go on the wire.
        # Keep the SDK's in-process streaming markers on its original request.
        outbound = httpx.Request(
            request.method,
            request.url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept-Encoding": "identity",
                "Content-Type": "application/json",
            },
            content=request.content,
            extensions=request.extensions,
        )
        self.sent = True
        response = self.transport.handle_request(outbound)
        if response.is_success:
            return response
        try:
            # The SDK otherwise buffers non-success responses before raising.
            read_provider_response(response, maximum=JSON_RESPONSE_LIMIT)
            raise GatewayFailure("INVALID_RESPONSE")
        finally:
            response.close()

    def close(self) -> None:
        self.transport.close()


def generate_image(
    *,
    base_url: str,
    url: str,
    api_key: str,
    parameters: dict,
    timeout: float,
    transport: httpx.BaseTransport,
) -> bytes:
    suppress_sdk_logging()
    try:
        with httpx.Client(
            transport=_ImageTransport(url, api_key, transport),
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
        ) as http:
            sdk = OpenAI(
                api_key=api_key,
                base_url=base_url,
                organization="",
                project="",
                admin_api_key="",
                http_client=http,
                timeout=timeout,
                max_retries=0,
            )
            with sdk.images.with_streaming_response.generate(**parameters) as response:
                # Retain the native JSON envelope and the existing media read contract.
                return read_provider_response(response.http_response, maximum=None)
    except OpenAIError as exc:
        cause = exc.__cause__
        if isinstance(cause, (GatewayFailure, DomainError, httpx.HTTPError)):
            raise cause from None
        raise GatewayFailure("INVALID_RESPONSE") from None
