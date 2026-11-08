"""Resolve, validate and pin public HTTPS destinations before attaching credentials."""

import re
import socket
import threading
from ipaddress import ip_address
from urllib.parse import urlsplit, urlunsplit

import httpx

from arclight.core.errors import DomainError

_DNS_SLOTS = threading.BoundedSemaphore(8)


def validate_endpoint(url: str) -> str:
    try:
        if any(ord(char) <= 32 or ord(char) == 127 for char in url) or "\\" in url:
            raise ValueError
        parts = urlsplit(url)
        host = (parts.hostname or "").rstrip(".").encode("idna").decode().lower()
        if (
            parts.scheme != "https"
            or not host
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or "?" in url
            or "#" in url
            or parts.port not in (None, 443)
            or "." not in host
            or len(host) > 253
            or host.endswith(
                (".localhost", ".local", ".internal", ".test", ".invalid", ".home.arpa")
            )
            or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in host.split(".")
            )
        ):
            raise ValueError
        try:
            ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError
        # libc also accepts legacy numeric IPv4 forms such as 0177.0.0.1.
        try:
            socket.inet_aton(host)
        except OSError:
            pass
        else:
            raise ValueError
        return urlunsplit(("https", host, parts.path.rstrip("/"), "", ""))
    except (ValueError, UnicodeError):
        raise DomainError(
            "INVALID_SERVICE_URL", "服务地址必须是无凭证和参数的公网 HTTPS 域名地址（443端口）", 422
        ) from None


def resolve_public(host: str, timeout: float) -> str:
    if not _DNS_SLOTS.acquire(blocking=False):
        raise DomainError("SERVICE_DNS_UNAVAILABLE", "服务域名解析繁忙，请稍后重试", 503)
    finished = threading.Event()
    answers: list[str] = []

    def resolve():
        try:
            answers.extend(
                str(item[4][0]) for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            )
        except OSError:
            pass
        finally:
            _DNS_SLOTS.release()
            finished.set()

    threading.Thread(target=resolve, daemon=True, name="model-service-dns").start()
    if not finished.wait(timeout) or not answers:
        raise DomainError("SERVICE_DNS_UNAVAILABLE", "服务域名解析失败或超时，请检查地址", 502)
    for address in answers:
        try:
            ip = ip_address(address)
            if (
                not ip.is_global
                or ip.is_multicast
                or ip.is_reserved
                or getattr(ip, "ipv4_mapped", None)
                or getattr(ip, "sixtofour", None)
                or getattr(ip, "teredo", None)
            ):
                raise ValueError
        except ValueError:
            raise DomainError(
                "SERVICE_ADDRESS_BLOCKED", "服务域名解析到非公网地址，已阻止请求", 422
            ) from None
    return answers[0]


class PublicHTTPTransport(httpx.BaseTransport):
    def __init__(self, dns_timeout: float = 5, *, allow_query: bool = False):
        self.dns_timeout = dns_timeout
        self.allow_query = allow_query
        self.inner = httpx.HTTPTransport(trust_env=False, retries=0)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        validate_endpoint(
            str(request.url.copy_with(query=None)) if self.allow_query else str(request.url)
        )
        hostname = request.url.host
        address = resolve_public(hostname, self.dns_timeout)
        # HTTPcore uses this hostname for TLS verification as well as SNI. The TCP
        # destination is the validated numeric IP, so no second DNS lookup occurs.
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=address),
            headers=request.headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": hostname},
        )
        response = self.inner.handle_request(pinned)
        if response.is_redirect:
            response.close()
            raise DomainError(
                "SERVICE_REDIRECT_BLOCKED", "模型服务返回重定向，请填写最终 HTTPS 服务地址", 502
            )
        return response

    def close(self) -> None:
        self.inner.close()
