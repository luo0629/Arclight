"""Operator-only credential check/rotation. Does not print or export any secret."""

import argparse
import json

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from arclight.application.service_credentials import rotate_credentials, verify_credentials
from arclight.core.config import get_settings
from arclight.core.errors import DomainError
from arclight.persistence.session import build_engine, session_factory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("check", "rotate"))
    parser.add_argument("--actor", default="", help="Operator identity for the immutable audit")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply a rotation after backing up keys and the database",
    )
    args = parser.parse_args()
    if args.operation == "rotate" and (
        not args.apply or not args.actor.strip() or len(args.actor) > 100
    ):
        parser.error("rotate requires --apply and an operator identity (--actor, 1-100 characters)")
    engine = None
    try:
        settings = get_settings()
        engine = build_engine(settings)
        with session_factory(engine).begin() as db:
            result = (
                rotate_credentials(db, settings, args.actor)
                if args.operation == "rotate"
                else verify_credentials(db, settings)
            )
        print(json.dumps({"status": "ok", **result}))
        return 0
    except DomainError as exc:
        print(json.dumps({"status": "error", "code": exc.code, "message": exc.message}))
        return 1
    except (ValidationError, SQLAlchemyError, OSError, ValueError):
        # Driver exceptions may include ciphertext in SQL parameters; never print them.
        print(json.dumps({"status": "error", "code": "KEY_STORAGE_CHECK_FAILED"}))
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
