"""隔离验收脚本的本机请求与资源边界回归测试，不启动真实容器。"""

import json
from unittest.mock import Mock

import httpx

from scripts import verify_compose


def test_loopback_probe_ignores_system_proxy(monkeypatch):
    calls = []
    config = {"services": {}, "volumes": {}}

    def docker(*args, capture=False):
        calls.append(args)
        if args == ("config", "--format", "json"):
            return json.dumps(config)
        if args[-3:] == ("port", "web", "80"):
            return "127.0.0.1:45678\n"
        return ""

    def respond(request):
        assert request.url.host == "127.0.0.1"
        assert request.url.port == 45678
        return httpx.Response(
            404 if request.url.path.startswith("/assets/") else 200,
            text='<div id="root"></div>',
            headers={
                "x-content-type-options": "nosniff",
                "x-frame-options": "DENY",
                "content-security-policy": "frame-ancestors 'none'",
                "cache-control": "no-cache",
            },
        )

    real_client = httpx.Client
    client_options = []

    def client(**kwargs):
        client_options.append(kwargs)
        return real_client(transport=httpx.MockTransport(respond), **kwargs)

    browser = Mock()
    monkeypatch.setattr(verify_compose, "docker", docker)
    monkeypatch.setattr(verify_compose.httpx, "Client", client)
    monkeypatch.setattr(verify_compose.subprocess, "run", browser)
    verify_compose.main()

    assert client_options == [
        {"base_url": "http://127.0.0.1:45678", "timeout": 10, "trust_env": False}
    ]
    browser.assert_called_once()
    assert browser.call_args.kwargs["env"]["ARCLIGHT_E2E_BASE_URL"] == ("http://127.0.0.1:45678")
    assert calls[-1][-4:] == ("down", "--volumes", "--timeout", "15")
    assert calls[-1][0] == "--project-name"
    assert calls[-1][1].startswith("arclight-verify-")
