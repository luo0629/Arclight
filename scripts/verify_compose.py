"""隔离容器验收：仅使用临时项目／测试卷、Mock Provider，结束后清理本次资源。"""

import json
import os
import secrets
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]


def docker(*args: str, capture: bool = False) -> str:
    result = subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout or ""


def main() -> None:
    project = "arclight-verify-" + uuid4().hex[:12]
    config = json.loads(docker("config", "--format", "json", capture=True))
    config["name"] = project
    config["networks"] = {"default": {"name": project + "_default"}}
    config["volumes"] = {
        name: {"name": project + "_" + name} for name in ("postgres-data", "redis-data")
    }
    jwt_secret = secrets.token_urlsafe(48)
    for name, service in config["services"].items():
        service.pop("build", None)
        service.pop("container_name", None)
        service.pop("ports", None)
        service["restart"] = "no"
        if name == "web":
            service["image"] = "arclight-web:latest"
            service["ports"] = [{"target": 80, "published": "0", "host_ip": "127.0.0.1"}]
        elif name not in ("postgres", "redis"):
            service["image"] = "arclight-api:latest"
            service["environment"].update(
                ARCLIGHT_ENVIRONMENT="development",
                ARCLIGHT_PROVIDER="mock",
                ARCLIGHT_LLM_API_KEY="",
                ARCLIGHT_JWT_SECRET=jwt_secret,
                ARCLIGHT_ALLOW_REGISTRATION="true",
                ARCLIGHT_AUTH_ALLOWED_ORIGINS="[]",
                ARCLIGHT_DATABASE_URL=(
                    "postgresql+psycopg://arclight:arclight@postgres:5432/arclight_test"
                ),
                ARCLIGHT_CHECKPOINT_URL=(
                    "postgresql://arclight:arclight@postgres:5432/arclight_test"
                ),
            )
        if name == "api":
            service["environment"]["FORWARDED_ALLOW_IPS"] = "*"
        if name == "postgres":
            service["environment"]["POSTGRES_DB"] = "arclight_test"
            service["healthcheck"]["test"] = [
                "CMD-SHELL",
                "pg_isready -U arclight -d arclight_test",
            ]
        # 不允许外部卷或 bind mount 混入验收，清理仅作用于本次随机项目。
        for mount in service.get("volumes", []):
            if mount["type"] != "volume" or mount["source"] not in config["volumes"]:
                raise RuntimeError("验收配置包含非隔离挂载，拒绝启动")
    with tempfile.TemporaryDirectory(prefix=project + "-") as folder:
        path = Path(folder) / "compose.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        args = ("--project-name", project, "--file", str(path))
        try:
            docker(*args, "up", "--detach", "--no-build", "--wait", "--wait-timeout", "180")
            docker(*args, "exec", "-T", "web", "nginx", "-t")
            port = docker(*args, "port", "web", "80", capture=True).strip().rsplit(":", 1)[1]
            base = "http://127.0.0.1:" + port
            with httpx.Client(base_url=base, timeout=10) as client:
                index = client.get("/")
                index.raise_for_status()
                assert 'id="root"' in index.text
                assert index.headers["x-content-type-options"] == "nosniff"
                assert index.headers["x-frame-options"] == "DENY"
                assert "frame-ancestors 'none'" in index.headers["content-security-policy"]
                assert index.headers["cache-control"] == "no-cache"
                missing = client.get("/assets/not-present.js")
                assert missing.status_code == 404
                assert missing.headers["x-content-type-options"] == "nosniff"
                client.get("/health/ready").raise_for_status()
            env = {**os.environ, "ARCLIGHT_E2E_BASE_URL": base}
            subprocess.run(
                ["npm.cmd" if os.name == "nt" else "npm", "run", "test:e2e"],
                cwd=ROOT / "apps/web",
                env=env,
                check=True,
            )
            print("Docker Nginx + API + PostgreSQL + Redis + Worker + Beat 验收通过")
        except BaseException:
            docker(*args, "logs", "--tail", "60")
            raise
        finally:
            # 文件内容由本进程生成，无自定义卷名；不接受用户传入的项目名／配置。
            if not project.startswith("arclight-verify-") or any(
                not volume["name"].startswith(project + "_")
                for volume in config["volumes"].values()
            ):
                raise RuntimeError("资源隔离检查失败，拒绝清理")
            docker(*args, "down", "--volumes", "--timeout", "15")


if __name__ == "__main__":
    main()
