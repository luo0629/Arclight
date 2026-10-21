"""浏览器验收专用：独立临时库与本地 Mock 执行器，不连接用户数据库。"""

import tempfile
import threading
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config
from arclight.application.runtime import execute_run
from arclight.core.config import Settings
from arclight.main import create_app
from arclight.persistence.models import WorkflowRun
from arclight.persistence.session import build_engine, session_factory
from sqlalchemy import select


def main():
    folder = Path(tempfile.mkdtemp(prefix="arclight-e2e-"))
    settings = Settings(
        _env_file=None,
        environment="test",
        provider="mock",
        database_url="sqlite:///" + (folder / "business.db").as_posix(),
        checkpoint_url="sqlite:///" + (folder / "checkpoints.db").as_posix(),
        auth_requests_per_minute=300,
    )
    config = Config("alembic.ini")
    config.attributes["database_url"] = settings.database_url
    command.upgrade(config, "head")
    factory = session_factory(build_engine(settings))
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
            for run_id, revision in jobs:
                execute_run(factory, settings, run_id, revision)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        uvicorn.run(create_app(settings), host="127.0.0.1", port=8766, log_level="warning")
    finally:
        stop.set()
        thread.join(timeout=10)
        factory.kw["bind"].dispose()


if __name__ == "__main__":
    main()
