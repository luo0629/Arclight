"""Plan and run scoped pre-push checks; incomplete evidence never counts as passed."""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
RISKS = ("migration", "concurrency", "storage", "broker", "media", "api")


@dataclass(frozen=True)
class Plan:
    root: Path
    base: str
    head: str
    changes: tuple[str, ...]
    fingerprints: dict[str, str | None]
    backend: tuple[str, ...]
    web: tuple[str, ...]
    risks: tuple[str, ...]
    issues: tuple[str, ...]
    database: str = "auto"
    web_mode: str = "defer"


@dataclass
class Evidence:
    passed: set[str] = field(default_factory=set)
    skipped: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode:
        raise ValueError("Git lookup failed; provide an existing commit with --base")
    return result.stdout.rstrip("\r\n")


def relevant(path: str) -> bool:
    return path.startswith(("backend/", "fronted/", ".github/", "infra/")) or path in {
        "compose.yml",
        "compose.yaml",
        "docker-compose.yml",
        ".gitignore",
    }


def fingerprints(root: Path) -> dict[str, str | None]:
    paths = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        if (root / name).is_file()
        else None
        for name in sorted(set(paths.split("\0")))
        if relevant(name)
    }


def backend_test(path: Path) -> bool:
    return path.suffix == ".py" and (
        path.name.startswith("test_") or path.name.endswith("_test.py")
    )


def web_test(path: Path) -> bool:
    return bool(re.search(r"\.(?:spec|test)\.[cm]?[jt]sx?$", path.name))


def test_path(root: Path, value: str, package: str) -> str:
    value = value.replace("\\", "/")
    if any(char in value for char in (":", "*", "?", "[", "]")):
        raise ValueError("Select whole test files, without node IDs, globs or filters")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Test files must be repository-relative paths")
    path = root / (value if value.startswith(package + "/") else f"{package}/{value}")
    if not path.is_file() or not path.resolve().is_relative_to(
        (root / package / "tests").resolve()
    ):
        raise ValueError(f"Selected {package} test file does not exist under its tests directory")
    if package == "backend" and not backend_test(path):
        raise ValueError("Backend selection must contain complete test_*.py or *_test.py files")
    if package == "fronted" and not web_test(path):
        raise ValueError("Browser selection must contain complete Playwright spec/test files")
    return path.relative_to(root).as_posix()


def make_plan(root: Path, args: argparse.Namespace) -> Plan:
    if not args.base or args.base.startswith("-"):
        raise ValueError("--base must identify an existing commit")
    base = git(root, "rev-parse", "--verify", "--end-of-options", args.base + "^{commit}")
    changed = git(root, "diff", "--name-only", "-z", base, "--").split("\0")
    changed += git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    changes = tuple(sorted({name for name in changed if relevant(name)}))
    backend = {test_path(root, name, "backend") for name in args.backend_test}
    web = {test_path(root, name, "fronted") for name in args.web_test}
    risks = set(args.risk)
    for name in changes:
        path = root / name
        if path.is_file() and name.startswith("backend/tests/") and backend_test(path):
            backend.add(test_path(root, name, "backend"))
        if path.is_file() and name.startswith("fronted/tests/") and web_test(path):
            web.add(test_path(root, name, "fronted"))
        if name.startswith("backend/migrations/") or (
            name.startswith("backend/tests/") and "migration" in path.name
        ):
            risks.add("migration")
    if "migration" in risks:
        backend.update(
            p.relative_to(root).as_posix()
            for p in (root / "backend/tests").rglob("*.py")
            if backend_test(p) and "migration" in p.name
        )
        for name in ("test_immutable_history.py", "test_content_history.py"):
            path = root / "backend/tests" / name
            if path.is_file():
                backend.add(path.relative_to(root).as_posix())
    issues = []
    if any(name.startswith("backend/") for name in changes) and not backend:
        issues.append("Backend changes require affected old and new modules via --backend-test")
    if any(name.startswith("fronted/") for name in changes) and not web:
        issues.append("Frontend changes require affected old and new specs via --web-test")
    if not backend and not web:
        issues.append("No runnable tests selected; an empty scope cannot pass preflight")
    if any(not (root / name).exists() and "/tests/" in name for name in changes):
        issues.append("Deleted tests require an impact review; they cannot count as executed")
    return Plan(
        root,
        base,
        git(root, "rev-parse", "HEAD"),
        changes,
        fingerprints(root),
        tuple(sorted(backend)),
        tuple(sorted(web)),
        tuple(sorted(risks)),
        tuple(issues),
        getattr(args, "database", "auto"),
        getattr(args, "web_mode", "defer"),
    )


def test_environment(plan: Plan) -> tuple[dict[str, str], list[str]]:
    environment = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("ARCLIGHT_", "PLAYWRIGHT_JSON_OUTPUT_"))
    }
    environment.update(PYTEST_ADDOPTS="", ARCLIGHT_PROVIDER="mock", ARCLIGHT_ENVIRONMENT="test")
    issues = []
    requirements = {}
    if set(plan.risks) & {"migration", "concurrency"} or plan.database in {"postgres", "both"}:
        requirements["ARCLIGHT_TEST_DATABASE_URL"] = "PostgreSQL arclight_test database"
    if "storage" in plan.risks:
        requirements["ARCLIGHT_TEST_S3_ENDPOINT"] = "dedicated S3 test service"
    if "broker" in plan.risks:
        requirements["ARCLIGHT_TEST_REDIS_URL"] = "loopback Redis database 15"
    for name, label in requirements.items():
        value = os.environ.get(name, "")
        if not value:
            if name == "ARCLIGHT_TEST_DATABASE_URL" and plan.database in {"postgres", "both"}:
                issues.append(
                    f"Missing {name}: {label}; no test services are started automatically"
                )
            continue
        parsed = urlsplit(value)
        if name == "ARCLIGHT_TEST_DATABASE_URL" and (
            parsed.scheme != "postgresql+psycopg" or parsed.path != "/arclight_test"
        ):
            issues.append("PostgreSQL checks require the dedicated arclight_test database")
        elif name == "ARCLIGHT_TEST_REDIS_URL" and (
            parsed.hostname not in {"localhost", "127.0.0.1"} or parsed.path != "/15"
        ):
            issues.append("Redis checks require loopback and dedicated database /15")
        else:
            environment[name] = value
    return environment, issues


def selected_databases(plan: Plan, environment: dict[str, str]) -> tuple[str, ...]:
    """Use one appropriate database locally unless both were explicitly requested."""
    if not plan.backend:
        return ()
    if plan.database == "both":
        return ("sqlite", "postgres")
    if plan.database != "auto":
        return (plan.database,)
    if set(plan.risks) & {"migration", "concurrency"} and environment.get(
        "ARCLIGHT_TEST_DATABASE_URL"
    ):
        return ("postgres",)
    return ("sqlite",)


def service_availability(environment: dict[str, str]) -> dict[str, bool]:
    """Record availability flags without retaining test connection secrets."""
    return {
        "storage": bool(environment.get("ARCLIGHT_TEST_S3_ENDPOINT")),
        "broker": bool(environment.get("ARCLIGHT_TEST_REDIS_URL")),
        "media": bool(shutil.which("ffprobe")),
    }


def environment_skip(reason: str, databases: tuple[str, ...], services: dict[str, bool]) -> bool:
    """Defer only recognized environment gaps recorded at execution time."""
    lowered = reason.lower()
    if any(
        marker in lowered for marker in ("bug", "disabled", "todo", "broken", "not implemented")
    ) or ("regression" in lowered and lowered != "sqlite replace bypass regression"):
        return False
    service_reasons = {
        "requires isolated s3 test service": "storage",
        "requires dedicated s3": "storage",
        "real redis requires arclight_test_redis_url on dedicated database 15": "broker",
        "independent ffprobe cli unavailable locally; required in final ci": "media",
    }
    if service := service_reasons.get(lowered):
        return services.get(service) is False
    dialect = re.search(r"postgres(?:ql)?|sqlite", lowered)
    if not dialect:
        return False
    database = "sqlite" if dialect.group() == "sqlite" else "postgres"
    if database in databases:
        return False
    return bool(
        re.match(r"(?:real |requires (?:real |isolated )?)?(?:postgresql|sqlite)\b", lowered)
        or re.search(r"(?:is |only on )(?:postgresql|sqlite)", lowered)
        or "需要真实 postgresql" in lowered
        or "必须使用真实 postgresql" in lowered
        or lowered == "production row-lock concurrency requires isolated postgresql"
        or lowered == "live harness deliberately requires an isolated sqlite fixture"
    )


def deferred_checks(
    plan: Plan, environment: dict[str, str], databases: tuple[str, ...]
) -> dict[str, str]:
    deferred = {}
    if set(plan.risks) & {"migration", "concurrency"} and "postgres" not in databases:
        deferred["postgres"] = "Real PostgreSQL validation is required in exact-SHA CI"
    for risk, variable in (
        ("storage", "ARCLIGHT_TEST_S3_ENDPOINT"),
        ("broker", "ARCLIGHT_TEST_REDIS_URL"),
    ):
        if risk in plan.risks and not environment.get(variable):
            deferred[risk] = "Dedicated test service validation is required in exact-SHA CI"
    if "media" in plan.risks and not shutil.which("ffprobe"):
        deferred["media"] = "Independent FFprobe validation is required in exact-SHA CI"
    if plan.web_mode == "defer":
        deferred.update(
            (f"browser:{name}", "Production Chromium execution is required in exact-SHA CI")
            for name in plan.web
        )
    return deferred


def junit_evidence(paths: list[Path], selected: tuple[str, ...]) -> Evidence:
    evidence = Evidence()
    seen: set[str] = set()
    passed_by_database: dict[str, set[str]] = {}
    skipped_cases = []
    for path in paths:
        database = path.stem.removeprefix("pytest-")
        try:
            cases = list(ET.parse(path).getroot().iter("testcase"))
            if not cases:
                evidence.missing.append(
                    f"{path.name}: no executed cases (collection is insufficient)"
                )
            for case in cases:
                classname = case.get("classname", "")
                name = classname + "::" + case.get("name", "")
                seen.update(p for p in selected if Path(p).stem in classname.split("."))
                if case.find("failure") is not None or case.find("error") is not None:
                    evidence.errors.append(name)
                elif (skip := case.find("skipped")) is not None:
                    skipped_cases.append(
                        (name, skip.get("message", "Skipped without a reason"), database)
                    )
                else:
                    evidence.passed.add(name)
                    passed_by_database.setdefault(database, set()).add(name)
        except (OSError, ET.ParseError):
            evidence.missing.append(f"{path.name}: missing or invalid JUnit report")
    evidence.missing.extend(f"No execution evidence for {p}" for p in selected if p not in seen)
    for name, reason, database in skipped_cases:
        counterpart = "postgres" if database == "sqlite" else "sqlite"
        if not (
            database in {"sqlite", "postgres"}
            and environment_skip(reason, (database,), {})
            and name in passed_by_database.get(counterpart, set())
        ):
            evidence.skipped[name] = reason
    return evidence


def browser_evidence(path: Path, selected: tuple[str, ...]) -> Evidence:
    evidence = Evidence()
    seen: set[str] = set()

    def visit(suites):
        for suite in suites:
            visit(suite.get("suites", []))
            for spec in suite.get("specs", []):
                filename = str(spec.get("file", suite.get("file", ""))).replace("\\", "/")
                for test in spec.get("tests", []):
                    name = f"{filename}::{spec.get('title')}::{test.get('projectName')}"
                    results = test.get("results", [])
                    if not results:
                        evidence.missing.append(name + ": no execution results")
                        continue
                    seen.update(
                        p
                        for p in selected
                        if (
                            filename.endswith(p.removeprefix("fronted/"))
                            or filename == p.removeprefix("fronted/tests/")
                        )
                    )
                    statuses = [result.get("status") for result in results]
                    if test.get("status") in {"flaky", "unexpected"} or any(
                        s not in {"passed", "skipped"} for s in statuses
                    ):
                        evidence.errors.append(name)
                    elif "skipped" in statuses:
                        reasons = [
                            a.get("description", "skip") for a in test.get("annotations", [])
                        ]
                        evidence.skipped[name] = "; ".join(reasons) or "Skipped without a reason"
                    else:
                        evidence.passed.add(name)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        visit(data["suites"])
        if data.get("errors"):
            evidence.errors.append("Playwright reported global errors")
        if data.get("stats", {}).get("flaky", 0):
            evidence.errors.append("Playwright reported flaky cases")
        if not evidence.passed and not evidence.skipped and not evidence.errors:
            evidence.missing.append("No executed browser cases; listing tests is insufficient")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        evidence.missing.append("Missing or invalid Playwright JSON execution report")
    evidence.missing.extend(f"No execution evidence for {p}" for p in selected if p not in seen)
    return evidence


def run_command(argv: list[str], cwd: Path, environment: dict[str, str], log: Path) -> int:
    with log.open("w", encoding="utf-8") as stream:
        try:
            return subprocess.run(
                argv, cwd=cwd, env=environment, stdout=stream, stderr=subprocess.STDOUT
            ).returncode
        except OSError:
            stream.write("Unable to start the required command; check installed tools.\n")
            return 1


def execute(
    plan: Plan, folder: Path, environment: dict[str, str]
) -> tuple[str, list, list[str], dict]:
    records = []
    reports = []
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm") or "npm"
    checks = []
    databases = selected_databases(plan, environment)
    deferred = deferred_checks(plan, environment, databases)
    services = service_availability(environment)
    if plan.backend:
        for args in (("ruff", "check", "."), ("ruff", "format", "--check", "."), ("mypy",)):
            checks.append((list(("uv", "run", "--frozen", *args)), "backend", environment))
        for database in databases:
            report = folder / f"pytest-{database}.xml"
            reports.append(report)
            env = environment | {
                "ARCLIGHT_TEST_DATABASE_URL": (
                    environment.get("ARCLIGHT_TEST_DATABASE_URL", "")
                    if database == "postgres"
                    else ""
                )
            }
            command = [
                "uv",
                "run",
                "--frozen",
                "pytest",
                "-o",
                "addopts=",
                "-q",
                "-ra",
                "-n",
                "auto",
                "--maxprocesses=4",
                "--dist=loadfile",
                f"--junitxml={report}",
            ]
            checks.append(
                (command + [p.removeprefix("backend/") for p in plan.backend], "backend", env)
            )
    if plan.web:
        checks.append(([npm, "run", "typecheck"], "fronted", environment))
        if plan.web_mode != "defer":
            env = environment | {
                "CI": "1",
                "ARCLIGHT_E2E_PRODUCTION": "1" if plan.web_mode == "production" else "0",
                "PLAYWRIGHT_JSON_OUTPUT_FILE": str(folder / "playwright.json"),
            }
            if plan.web_mode == "production":
                checks.append(([npm, "run", "build"], "fronted", env))
            checks.append(
                (
                    [
                        npm,
                        "run",
                        "test:e2e",
                        "--",
                        "--reporter=line,json",
                        "--forbid-only",
                        *[p.removeprefix("fronted/") for p in plan.web],
                    ],
                    "fronted",
                    env,
                )
            )
    for index, (command, package, env) in enumerate(checks):
        print(f"[{index + 1}/{len(checks)}] {package}: {' '.join(command)}", flush=True)
        code = run_command(command, plan.root / package, env, folder / f"command-{index + 1}.log")
        records.append({"argv": command, "cwd": str(plan.root / package), "exit_code": code})
        if code:
            return "failed", records, ["A required command failed; see its log"], {}
    evidence = []
    if plan.backend:
        result = junit_evidence(reports, plan.backend)
        for name, reason in list(result.skipped.items()):
            if environment_skip(reason, databases, services):
                deferred[f"case:{name}"] = reason
                del result.skipped[name]
        evidence.append(result)
    if plan.web and plan.web_mode != "defer":
        evidence.append(browser_evidence(folder / "playwright.json", plan.web))
    errors = [error for result in evidence for error in result.errors]
    missing = [item for result in evidence for item in result.missing]
    skips = {name: reason for result in evidence for name, reason in result.skipped.items()}
    status = "failed" if errors else "incomplete" if missing or skips else "passed"
    issues = [
        *errors,
        *missing,
        *[f"Unresolved skip: {name}: {reason}" for name, reason in skips.items()],
    ]
    return (
        status,
        records,
        issues,
        {
            "passed": sum(len(e.passed) for e in evidence),
            "skipped": skips,
            "databases": databases,
            "services": services,
            "deferred_checks": deferred,
        },
    )


def git_path(root: Path, name: str) -> Path:
    return Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", name))


def save_summary(root: Path, folder: Path, summary: dict) -> None:
    """Publish the latest attempt, including plans and failures, to the push guard."""
    summary["report_hashes"] = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(folder.iterdir())
        if path.is_file() and path.name != "summary.json"
    }
    path = folder / "summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    receipt = git_path(root, "arclight-preflight.json")
    temporary = receipt.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"summary": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}),
        encoding="utf-8",
    )
    temporary.replace(receipt)


def install_hook(root: Path) -> Path:
    """Install a worktree-aware hook without replacing an unrelated hook."""
    marker = "# Arclight scoped preflight gate"
    path = git_path(root, "hooks/pre-push")
    if path.exists() and marker not in path.read_text(encoding="utf-8"):
        raise ValueError("An existing pre-push hook must be integrated without overwriting it")
    interpreter = shlex.quote(Path(sys.executable).as_posix())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"#!/bin/sh\n{marker}\n"
        "worktree_root=$(git rev-parse --show-toplevel) || exit 2\n"
        f'exec {interpreter} "$worktree_root/backend/scripts/preflight.py" --verify-push\n',
        encoding="utf-8",
        newline="\n",
    )
    path.chmod(path.stat().st_mode | 0o111)
    return path


def verify_push(root: Path, updates: str = "") -> None:
    """Validate executed reports against the committed files and every pushed ref."""
    refs = [line.split() for line in updates.splitlines() if line.strip()]
    if any(len(ref) != 4 for ref in refs):
        raise ValueError("Invalid pre-push ref input")
    refs = [ref for ref in refs if set(ref[1]) != {"0"}]
    if updates.strip() and not refs:
        return  # Deleting a remote ref sends no new code.
    head = git(root, "rev-parse", "HEAD")
    if any(ref[1] != head for ref in refs):
        raise ValueError("Validate and push the current HEAD; other refs need their own checkout")
    receipt = json.loads(git_path(root, "arclight-preflight.json").read_text(encoding="utf-8"))
    path = Path(receipt["summary"])
    if hashlib.sha256(path.read_bytes()).hexdigest() != receipt["sha256"]:
        raise ValueError("Preflight summary changed; rerun the checks")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("status") != "passed" or data.get("issues"):
        raise ValueError(
            "Preflight must be passed; planned, failed and incomplete cannot be pushed"
        )
    commands = data.get("commands", [])
    if not commands or any(record.get("exit_code") != 0 for record in commands):
        raise ValueError("Preflight has no successful command execution evidence")
    pending = git(root, "diff", "--name-only", "-z", "HEAD", "--").split("\0")
    pending += git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    if any(relevant(name) for name in pending):
        raise ValueError("Commit the tested source and test files before pushing")
    if fingerprints(root) != data.get("fingerprints"):
        raise ValueError("Source changed since preflight; rerun the checks against the final files")
    plan = make_plan(
        root,
        argparse.Namespace(
            base=data["base"],
            backend_test=data["backend_tests"],
            web_test=data["web_tests"],
            risk=data["risks"],
            database=data.get("database", "auto"),
            web_mode=data.get("web_mode", "defer"),
        ),
    )
    if (
        plan.issues
        or plan.backend != tuple(data["backend_tests"])
        or plan.web != tuple(data["web_tests"])
    ):
        raise ValueError(
            "Preflight no longer covers all changed tests; regenerate and run the plan"
        )
    git(root, "merge-base", "--is-ancestor", plan.base, head)
    git(root, "merge-base", "--is-ancestor", data["head"], head)
    for ref in refs:
        if set(ref[3]) != {"0"}:
            try:
                git(root, "merge-base", "--is-ancestor", plan.base, ref[3])
            except ValueError as error:
                raise ValueError(
                    "Preflight base must precede the remote commit; fetch and use the task baseline"
                ) from error
    hashes = data.get("report_hashes", {})
    databases = tuple(data.get("evidence", {}).get("databases", ()))
    if plan.database not in {"auto", "sqlite", "postgres", "both"} or plan.web_mode not in {
        "defer",
        "development",
        "production",
    }:
        raise ValueError("Invalid local verification mode; rerun preflight")
    expected = ("sqlite", "postgres") if plan.database == "both" else (plan.database,)
    if plan.backend and (
        (plan.database == "auto" and databases not in {("sqlite",), ("postgres",)})
        or (plan.database != "auto" and databases != expected)
    ):
        raise ValueError("Missing actual local database execution scope")
    required = [f"pytest-{database}.xml" for database in databases]
    if plan.web:
        if not any(record["argv"][1:] == ["run", "typecheck"] for record in commands):
            raise ValueError("Missing frontend typecheck execution")
        if plan.web_mode != "defer":
            required.append("playwright.json")
    if not hashes or any(name not in hashes for name in required):
        raise ValueError("Missing actual JUnit or browser execution reports")
    for name, digest in hashes.items():
        report = path.parent / name
        if report.name != name or hashlib.sha256(report.read_bytes()).hexdigest() != digest:
            raise ValueError("Execution report or command log changed; rerun the checks")
    deferred = data.get("deferred_checks", {})
    if plan.web_mode == "defer" and any(f"browser:{name}" not in deferred for name in plan.web):
        raise ValueError("Missing exact-SHA CI deferral for browser tests")
    if (
        set(plan.risks) & {"migration", "concurrency"}
        and "postgres" not in databases
        and "postgres" not in deferred
    ):
        raise ValueError("Missing exact-SHA CI deferral for PostgreSQL")
    evidence = []
    if plan.backend:
        result = junit_evidence(
            [path.parent / name for name in required if name.endswith(".xml")], plan.backend
        )
        for name, reason in list(result.skipped.items()):
            if (
                environment_skip(reason, databases, data.get("evidence", {}).get("services", {}))
                and deferred.get(f"case:{name}") == reason
            ):
                del result.skipped[name]
        evidence.append(result)
    if plan.web and plan.web_mode != "defer":
        evidence.append(browser_evidence(path.parent / "playwright.json", plan.web))
    if any(e.errors or e.missing or e.skipped for e in evidence):
        raise ValueError("Execution evidence contains failures, missing cases or unresolved skips")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--base", help="Commit before this change, not an empty post-push diff")
    result.add_argument("--backend-test", action="append", default=[], metavar="tests/test_file.py")
    result.add_argument("--web-test", action="append", default=[], metavar="tests/file.spec.ts")
    result.add_argument("--risk", action="append", choices=RISKS, default=[])
    result.add_argument(
        "--database",
        choices=("auto", "sqlite", "postgres", "both"),
        default="auto",
        help="Local database; auto prefers PostgreSQL for migration/concurrency when configured",
    )
    result.add_argument(
        "--web-mode",
        choices=("defer", "development", "production"),
        default="defer",
        help="Default runs local typecheck and defers production browser execution to exact-SHA CI",
    )
    result.add_argument("--output", default="backend/test-results/preflight")
    mode = result.add_mutually_exclusive_group()
    mode.add_argument(
        "--run", action="store_true", help="Execute checks, including temporary browser services"
    )
    mode.add_argument(
        "--install-hook", action="store_true", help="Install the local pre-push guard"
    )
    mode.add_argument(
        "--verify-push", action="store_true", help="Check fresh execution evidence only"
    )
    return result


def main(argv: list[str] | None = None, *, root: Path = ROOT) -> int:
    args = parser().parse_args(argv)
    if args.install_hook or args.verify_push:
        try:
            if args.install_hook:
                print(f"Installed pre-push guard: {install_hook(root)}")
            else:
                verify_push(root, "" if sys.stdin.isatty() else sys.stdin.read())
                print("Pre-push execution evidence passed")
            return 0
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            print(
                f"Push blocked: {error}. Run preflight with the task --base and --run.",
                file=sys.stderr,
            )
            return 2
    folder = (root / args.output).resolve() / uuid4().hex
    folder.mkdir(parents=True, exist_ok=False)
    summary: dict = {"status": "incomplete", "commands": [], "issues": []}
    try:
        plan = make_plan(root, args)
        environment, missing = test_environment(plan)
        summary.update(
            base=plan.base,
            head=plan.head,
            changes=plan.changes,
            fingerprints=plan.fingerprints,
            backend_tests=plan.backend,
            web_tests=plan.web,
            risks=plan.risks,
            issues=[*plan.issues, *missing],
            database=plan.database,
            web_mode=plan.web_mode,
            ci_required=True,
            deferred_checks=deferred_checks(
                plan, environment, selected_databases(plan, environment)
            ),
        )
        if not args.run:
            summary["status"] = "planned"
        elif not summary["issues"]:
            status, records, errors, evidence = execute(plan, folder, environment)
            summary.update(status=status, commands=records, issues=errors, evidence=evidence)
            summary["deferred_checks"].update(evidence.get("deferred_checks", {}))
            if (
                fingerprints(root) != plan.fingerprints
                or git(root, "rev-parse", "HEAD") != plan.head
            ):
                if summary["status"] != "failed":
                    summary["status"] = "incomplete"
                summary["issues"].append(
                    "Source changed during preflight; rerun against the final files"
                )
    except (ValueError, OSError) as error:
        summary["issues"] = [str(error)]
    except KeyboardInterrupt:
        summary["issues"] = ["Preflight interrupted; execution evidence is incomplete"]
    try:
        save_summary(root, folder, summary)
    except (OSError, ValueError) as error:
        print(f"Could not publish pre-push evidence: {error}", file=sys.stderr)
        return 2
    print(f"Preflight {summary['status']}: {folder / 'summary.json'}")
    print("Backend:", ", ".join(summary.get("backend_tests", [])) or "none")
    print("Browser:", ", ".join(summary.get("web_tests", [])) or "none")
    print("Risks:", ", ".join(summary.get("risks", [])) or "none")
    for issue in summary["issues"]:
        print(issue)
    for name, reason in summary.get("deferred_checks", {}).items():
        print(f"Deferred to exact-SHA CI: {name}: {reason}")
    return {"planned": 0, "passed": 0, "failed": 1, "incomplete": 2}[summary["status"]]


if __name__ == "__main__":
    sys.exit(main())
