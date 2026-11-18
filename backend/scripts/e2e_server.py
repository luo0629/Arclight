"""浏览器验收专用：独立临时库与本地 Mock 执行器，不连接用户数据库。"""

import os
import tempfile
import threading
from decimal import Decimal
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select

from arclight.application.runtime import execute_run
from arclight.core.config import Settings
from arclight.domain.service_models import ServiceModelPolicy
from arclight.main import create_app
from arclight.persistence.models import Episode, WorkflowRun
from arclight.persistence.session import build_engine, session_factory
from arclight.storage.objects import ObjectStorage, initialize


def main():
    from e2e_screenwriter import install

    install()
    folder = Path(tempfile.mkdtemp(prefix="arclight-e2e-"))
    settings = Settings(
        _env_file=None,
        environment="test",
        provider="mock",
        # The frozen route does not persist the server key; workers use this fixture key.
        llm_api_key=SecretStr("offline-only"),
        service_encryption_key=Fernet.generate_key().decode(),
        database_url="sqlite:///" + (folder / "business.db").as_posix(),
        checkpoint_url="sqlite:///" + (folder / "checkpoints.db").as_posix(),
        auth_requests_per_minute=300,
        asset_storage_path=folder / "asset-objects",
        service_model_policies=[
            ServiceModelPolicy(
                kind="video",
                supplier="custom",
                protocol="seedance",
                model="doubao-seedance-1-0-pro-250528",
                base_url="https://native.example/v1",
                maximum_cost_per_output=Decimal("1"),
                pricing_version="video01-e2e:1",
            )
        ],
    )
    test_s3 = os.getenv("ARCLIGHT_E2E_S3_ENDPOINT")
    if test_s3:
        from uuid import uuid4

        settings.object_storage_backend = "s3"
        settings.s3_endpoint_url = test_s3
        settings.s3_access_key = SecretStr("arclight-test")
        settings.s3_secret_key = SecretStr("arclight-test-secret")
        settings.s3_bucket = "arclight-test-e2e-" + uuid4().hex
        initialize(settings)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["database_url"] = settings.database_url
    command.upgrade(config, "head")
    factory = session_factory(build_engine(settings), settings)
    stop = threading.Event()

    def worker():
        while not stop.wait(0.25):
            with factory() as db:
                jobs = [
                    (row.id, row.revision)
                    for row in db.scalars(
                        select(WorkflowRun).where(WorkflowRun.status == "QUEUED").limit(10)
                    )
                ]
                from arclight.application.producer_interaction import pending_commands

                for row in db.scalars(
                    select(WorkflowRun).where(
                        WorkflowRun.workflow_type == "producer", WorkflowRun.status == "RUNNING"
                    )
                ):
                    episode = db.get(Episode, row.episode_id)
                    if episode and pending_commands(db, row, episode.project_id):
                        jobs.append((row.id, row.revision))
            for run_id, revision in jobs:
                execute_run(factory, settings, run_id, revision)
            from e2e_art import advance_concepts

            advance_concepts(factory, settings)
            from e2e_video import advance_renders, advance_videos

            advance_videos(factory, settings)
            advance_renders(factory, settings)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        uvicorn.run(
            create_app(settings),
            host="127.0.0.1",
            port=int(os.environ.get("ARCLIGHT_E2E_API_PORT", "8766")),
            log_level="warning",
        )
    finally:
        stop.set()
        thread.join(timeout=10)
        factory.kw["bind"].dispose()
        if test_s3:
            store = ObjectStorage(settings)
            assert settings.environment == "test" and settings.s3_bucket.startswith(
                "arclight-test-e2e-"
            )
            for batch in store.client.get_paginator("list_object_versions").paginate(
                Bucket=settings.s3_bucket
            ):
                for item in batch.get("Versions", []) + batch.get("DeleteMarkers", []):
                    store.client.delete_object(
                        Bucket=settings.s3_bucket, Key=item["Key"], VersionId=item["VersionId"]
                    )
            store.client.delete_bucket(Bucket=settings.s3_bucket)


if __name__ == "__main__":
    main()
