"""Custom provider egress: validate once, resolve all answers and pin the TLS peer."""

import json
import socket
import ssl
import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from arclight.agents.story_analysis import StoryAnalysisContent
from arclight.core.errors import DomainError
from arclight.domain.model_gateway import GatewayFailure
from arclight.domain.model_suppliers import SUPPLIERS
from arclight.providers import network
from arclight.providers.text import OpenAICompatibleTextProvider


@pytest.mark.parametrize(
    "address",
    [
        "http://api.example/v1",
        "https://127.0.0.1",
        "https://[::1]",
        "https://localhost",
        "https://api.internal/v1",
        "https://api.local/v1",
        "https://api.home.arpa/v1",
        "https://api.example:8443",
        "https://user:secret@api.example",
        "https://api.example?token=secret",
        "https://api.example#fragment",
        "https://api.example?",
        "https://api.example#",
        "https://api.example\\@other.example",
        "https://api.example/\npath",
        "https://2130706433",
        "https://0177.0.0.1",
        "https://0x7f.1",
        "https://0x7f000001",
        "https://-invalid.example",
        "https://api..example",
    ],
)
def test_invalid_endpoints_are_rejected_without_echo(address):
    with pytest.raises(DomainError) as caught:
        network.validate_endpoint(address)
    assert caught.value.code == "INVALID_SERVICE_URL"
    assert "secret" not in caught.value.message


def test_custom_endpoint_normalization_has_no_supplier_allowlist():
    assert (
        network.validate_endpoint("https://ANY-SUPPLIER.Example.:443/api/v1/")
        == "https://any-supplier.example/api/v1"
    )


@pytest.mark.parametrize(
    "addresses,allowed",
    [
        (["8.8.8.8"], True),
        (["2606:4700:4700::1111"], True),
        (["127.0.0.1"], False),
        (["10.0.0.1"], False),
        (["172.16.1.2"], False),
        (["192.168.0.1"], False),
        (["169.254.169.254"], False),
        (["100.100.100.200"], False),
        (["0.0.0.0"], False),
        (["224.0.0.1"], False),
        (["::1"], False),
        (["fc00::1"], False),
        (["fe80::1"], False),
        (["ff02::1"], False),
        (["::ffff:8.8.8.8"], False),
        (["2002:0808:0808::1"], False),
        (["8.8.8.8", "10.0.0.1"], False),
        (["8.8.8.8", "::1"], False),
    ],
)
def test_all_dns_answers_must_be_public(monkeypatch, addresses, allowed):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (
                socket.AF_INET6 if ":" in item else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (item, 443),
            )
            for item in addresses
        ],
    )
    if allowed:
        assert network.resolve_public("api.example", 1) == addresses[0]
    else:
        with pytest.raises(DomainError) as caught:
            network.resolve_public("api.example", 1)
        assert caught.value.code == "SERVICE_ADDRESS_BLOCKED"


def test_dns_timeout_and_worker_bound_fail_closed(monkeypatch):
    release, finished = threading.Event(), threading.Event()

    def slow_dns(*args, **kwargs):
        try:
            assert release.wait(5)
            return []
        finally:
            finished.set()

    monkeypatch.setattr(socket, "getaddrinfo", slow_dns)
    monkeypatch.setattr(network, "_DNS_SLOTS", threading.BoundedSemaphore(1))
    try:
        with pytest.raises(DomainError) as caught:
            network.resolve_public("api.example", 0.01)
        assert caught.value.code == "SERVICE_DNS_UNAVAILABLE"
        with pytest.raises(DomainError) as caught:
            network.resolve_public("api.example", 0.01)
        assert caught.value.status_code == 503
    finally:
        release.set()
        assert finished.wait(5)


@pytest.mark.parametrize("redirect", [False, True])
def test_transport_pins_ip_preserves_host_and_blocks_redirect(monkeypatch, redirect):
    dns, sent = [], []

    def resolve(host, timeout):
        dns.append(host)
        # A second resolution would represent rebinding to an internal address.
        return "8.8.8.8" if len(dns) == 1 else "127.0.0.1"

    def send(request):
        sent.append(request)
        return httpx.Response(
            307 if redirect else 200,
            headers={"Location": "https://private.example/"},
            content=b"{}",
        )

    monkeypatch.setattr(network, "resolve_public", resolve)
    transport = network.PublicHTTPTransport()
    transport.inner.close()
    transport.inner = httpx.MockTransport(send)
    try:
        with httpx.Client(transport=transport, trust_env=False, follow_redirects=False) as client:
            if redirect:
                with pytest.raises(DomainError) as caught:
                    client.post(
                        "https://api.example/v1/chat/completions",
                        headers={"Authorization": "Bearer test-only-key"},
                    )
                assert caught.value.code == "SERVICE_REDIRECT_BLOCKED"
            else:
                assert client.post("https://api.example/v1/chat/completions").status_code == 200
        assert dns == ["api.example"] and len(sent) == 1
        assert sent[0].url.host == "8.8.8.8"
        assert sent[0].headers["host"] == "api.example"
        assert sent[0].extensions["sni_hostname"] == "api.example"
    finally:
        transport.close()


def test_real_tls_validates_original_hostname_with_pinned_ip(monkeypatch, tmp_path):
    # Trusted local certificate, real TCP/TLS/HTTPcore; only socket routing is local.
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "api.example")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("api.example")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(private, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_file, key_file)
    sni, requests, targets, errors = [], [], [], []
    server_context.set_servername_callback(lambda sock, name, context: sni.append(name))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(5)

        def serve():
            try:
                peer, _ = listener.accept()
                with peer, server_context.wrap_socket(peer, server_side=True) as secured:
                    secured.settimeout(5)
                    data = b""
                    while b"\r\n\r\n" not in data:
                        data += secured.recv(4096)
                    requests.append(data)
                    secured.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
                    )
            except Exception as exc:
                errors.append(type(exc).__name__)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        original_connect = socket.create_connection

        def local_connection(address, *args, **kwargs):
            targets.append(address)
            assert address == ("8.8.8.8", 443)
            return original_connect(listener.getsockname(), *args, **kwargs)

        monkeypatch.setattr(socket, "create_connection", local_connection)
        monkeypatch.setattr(network, "resolve_public", lambda host, timeout: "8.8.8.8")
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
        transport = network.PublicHTTPTransport()
        transport.inner.close()
        context = ssl.create_default_context(cafile=str(cert_file))
        transport.inner = httpx.HTTPTransport(verify=context, trust_env=False, retries=0)
        try:
            with httpx.Client(transport=transport, trust_env=False) as client:
                assert client.get("https://api.example/v1").json() == {}
        finally:
            thread.join(timeout=6)
        assert not thread.is_alive() and not errors
        assert targets == [("8.8.8.8", 443)] and sni == ["api.example"]
        assert b"Host: api.example\r\n" in requests[0]


@pytest.mark.parametrize("supplier", SUPPLIERS, ids=lambda preset: preset.id)
def test_official_request_contract_and_invalid_json_rejection(monkeypatch, settings, supplier):
    sent = []
    valid = StoryAnalysisContent(
        schema_version="1.0",
        summary="test",
        conflicts=[],
        characters=[],
        key_events=[{"title": "event", "description": "one event", "character_names": []}],
    )
    responses = [valid.model_dump_json(), '{"summary": 123}']

    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": responses.pop(0)}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 8},
            },
        )

    monkeypatch.setattr(
        "arclight.providers.text.PublicHTTPTransport", lambda *args: httpx.MockTransport(respond)
    )
    configuration = settings.model_copy(
        update={
            "llm_supplier": supplier.id,
            "llm_response_format": supplier.response_format,
            "llm_model": supplier.model,
            "llm_base_url": supplier.base_url,
        }
    )
    provider = OpenAICompatibleTextProvider(configuration)
    assert provider.analyze("story") == valid
    body = sent[0]
    assert body["response_format"]["type"] == supplier.response_format
    assert body["model"] == supplier.model
    if supplier.id == "openai":
        assert "max_completion_tokens" in body and "max_tokens" not in body
        assert (
            body["response_format"]["json_schema"]["schema"]
            == StoryAnalysisContent.model_json_schema()
        )
    else:
        assert "max_tokens" in body and "JSON Schema" in body["messages"][0]["content"]
    assert (body.get("thinking") == {"type": "disabled"}) == (supplier.id == "deepseek")
    assert provider.last_usage.input_tokens == 4
    with pytest.raises(GatewayFailure) as caught:
        provider.analyze("story")
    assert caught.value.code == "INVALID_RESPONSE"
    assert caught.value.completed and not caught.value.not_accepted
    assert caught.value.usage == provider.last_usage
    assert caught.value.usage.input_tokens == 4
    assert caught.value.usage.output_tokens == 8
    assert caught.value.usage.actual_cost is None
    assert str(caught.value) == "Model service call failed"
    assert len(sent) == 2
