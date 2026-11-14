"""Plan and run scoped pre-push checks; incomplete evidence never counts as passed."""

import argparse
import hashlib
import json
import os
import re
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
    if set(plan.risks) & {"migration", "concurrency"}:
        requirements["ARCLIGHT_TEST_DATABASE_URL"] = "PostgreSQL arclight_test database"
    if "storage" in plan.risks:
        requirements["ARCLIGHT_TEST_S3_ENDPOINT"] = "dedicated S3 test service"
    if "broker" in plan.risks:
        requirements["ARCLIGHT_TEST_REDIS_URL"] = "loopback Redis database 15"
    for name, label in requirements.items():
        value = os.environ.get(name, "")
        if not value:
            issues.append(f"Missing {name}: {label}; no test services are started automatically")
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
    if "media" in plan.risks and not shutil.which("ffprobe"):
        issues.append("Missing ffprobe: independent media verification remains incomplete")
    return environment, issues


def junit_evidence(paths: list[Path], selected: tuple[str, ...]) -> Evidence:
    evidence = Evidence()
    seen = set()
    for path in paths:
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
                    evidence.skipped[name] = skip.get("message", "Skipped without a reason")
                else:
                    evidence.passed.add(name)
        except (OSError, ET.ParseError):
            evidence.missing.append(f"{path.name}: missing or invalid JUnit report")
    evidence.missing.extend(f"No execution evidence for {p}" for p in selected if p not in seen)
    evidence.skipped = {k: v for k, v in evidence.skipped.items() if k not in evidence.passed}
    return evidence


def browser_evidence(path: Path, selected: tuple[str, ...]) -> Evidence:
    evidence = Evidence()
    seen = set()

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
    if plan.backend:
        for args in (("ruff", "check", "."), ("ruff", "format", "--check", "."), ("mypy",)):
            checks.append((list(("uv", "run", "--frozen", *args)), "backend", environment))
        for database in (
            ("sqlite", "postgres")
            if set(plan.risks) & {"migration", "concurrency"}
            else ("sqlite",)
        ):
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
        env = environment | {
            "CI": "1",
            "ARCLIGHT_E2E_PRODUCTION": "1",
            "PLAYWRIGHT_JSON_OUTPUT_FILE": str(folder / "playwright.json"),
        }
        checks.extend(
            (
                ([npm, "run", "build"], "fronted", env),
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
                ),
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
        evidence.append(junit_evidence(reports, plan.backend))
    if plan.web:
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
        {"passed": sum(len(e.passed) for e in evidence), "skipped": skips},
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--base", required=True, help="Commit before this change, not an empty post-push diff"
    )
    result.add_argument("--backend-test", action="append", default=[], metavar="tests/test_file.py")
    result.add_argument("--web-test", action="append", default=[], metavar="tests/file.spec.ts")
    result.add_argument("--risk", action="append", choices=RISKS, default=[])
    result.add_argument("--output", default="backend/test-results/preflight")
    result.add_argument(
        "--run", action="store_true", help="Execute checks, including temporary browser services"
    )
    return result


def main(argv: list[str] | None = None, *, root: Path = ROOT) -> int:
    args = parser().parse_args(argv)
    folder = (root / args.output).resolve() / uuid4().hex
    folder.mkdir(parents=True, exist_ok=False)
    summary = {"status": "incomplete", "commands": [], "issues": []}
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
            browser_mode="production/chromium",
        )
        if not args.run:
            summary["status"] = "planned"
        elif not summary["issues"]:
            status, records, errors, evidence = execute(plan, folder, environment)
            summary.update(status=status, commands=records, issues=errors, evidence=evidence)
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
    (folder / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Preflight {summary['status']}: {folder / 'summary.json'}")
    print("Backend:", ", ".join(summary.get("backend_tests", [])) or "none")
    print("Browser:", ", ".join(summary.get("web_tests", [])) or "none")
    print("Risks:", ", ".join(summary.get("risks", [])) or "none")
    for issue in summary["issues"]:
        print(issue)
    return {"planned": 0, "passed": 0, "failed": 1, "incomplete": 2}[summary["status"]]


if __name__ == "__main__":
    sys.exit(main())
