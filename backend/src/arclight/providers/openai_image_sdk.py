"""Single-submit Images SDK transport for account and server routes."""

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import httpx
from openai import OpenAI, OpenAIError, omit

from arclight.core.errors import DomainError
from arclight.domain.model_gateway import GatewayFailure
from arclight.providers.media_io import JSON_RESPONSE_LIMIT, read_provider_response
from arclight.providers.sdk_logging import suppress_sdk_logging


def image_response_request_id(response: httpx.Response, api_key: str) -> str | None:
    header = response.headers.get("x-request-id", "")
    if re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", header) and api_key not in header:
        return header
    return None


def _validate_account_response(response: httpx.Response) -> None:
    if not response.is_success:
        read_provider_response(response, maximum=JSON_RESPONSE_LIMIT)
        raise GatewayFailure("INVALID_RESPONSE")


class _ImageTransport(httpx.BaseTransport):
    def __init__(
        self,
        url: str,
        api_key: str,
        transport: httpx.BaseTransport,
        validate_response: Callable[[httpx.Response], None] = _validate_account_response,
    ):
        self.url = httpx.URL(url)
        self.api_key = api_key
        self.transport = transport
        self.validate_response = validate_response
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
        try:
            response = self.transport.handle_request(outbound)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            # This transport makes a single attempt without proxying or retries.
            # These failures precede writing the POST; write/read failures do not.
            raise GatewayFailure("PROVIDER_UNAVAILABLE", not_accepted=True) from None
        except DomainError as exc:
            if exc.code not in {
                "INVALID_SERVICE_URL",
                "SERVICE_ADDRESS_BLOCKED",
                "SERVICE_DNS_UNAVAILABLE",
            }:
                raise
            # Public HTTP transport raises these while validating/resolving the URL.
            # A blocked redirect happens after sending and is deliberately excluded.
            raise GatewayFailure(exc.code, not_accepted=True) from None
        try:
            # Validate before the SDK can read an error body or remap a provider status.
            self.validate_response(response)
        except BaseException:
            response.close()
            raise
        return response

    def close(self) -> None:
        self.transport.close()


@contextmanager
def stream_image_generation(
    *,
    base_url: str,
    api_key: str,
    parameters: dict,
    timeout: float,
    transport: httpx.BaseTransport,
    path: str = "/images/generations",
    validate_response: Callable[[httpx.Response], None] = _validate_account_response,
) -> Iterator[httpx.Response]:
    suppress_sdk_logging()
    try:
        with httpx.Client(
            transport=_ImageTransport(base_url + path, api_key, transport, validate_response),
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
            # Default headers can alter SDK serialization before the transport sees the request.
            with sdk.images.with_streaming_response.generate(
                **parameters, extra_headers={name: omit for name in sdk.default_headers}
            ) as response:
                yield response.http_response
    except OpenAIError as exc:
        cause = exc.__cause__
        if isinstance(cause, (GatewayFailure, DomainError, httpx.HTTPError)):
            raise cause from None
        raise GatewayFailure("INVALID_RESPONSE") from None
