import argparse
from uuid import UUID

from arclight.application.runtime import execute_run
from arclight.core.config import get_settings
from arclight.persistence.session import build_engine, session_factory


def main() -> None:
    parser = argparse.ArgumentParser(description="单进程本地工作流执行器，不依赖 Redis")
    parser.add_argument("run_id", type=UUID)
    args = parser.parse_args()
    settings = get_settings()
    engine = build_engine(settings)
    try:
        factory = session_factory(engine)
        from arclight.persistence.models import WorkflowRun

        with factory() as db:
            run = db.get(WorkflowRun, args.run_id)
            if run is None:
                parser.error("工作流不存在")
            revision = run.revision
        execute_run(factory, settings, args.run_id, revision)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
