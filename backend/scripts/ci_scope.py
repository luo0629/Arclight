"""Choose a conservative CI scope and count tasks after a verified full checkpoint.

This script uses only the standard library, so the planning job can run before
installing project dependencies. Missing history or Actions evidence selects full
regression; commit trailers can request more verification, never less.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[2]
TASK = re.compile(r"^Arclight-Task:\s*([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)\s*$", re.MULTILINE)
FULL = re.compile(r"^Arclight-CI:\s*full\s*$", re.MULTILINE | re.IGNORECASE)
WEB_TEST = re.compile(r"fronted/tests/[\w/-]+\.(?:spec|test)\.[cm]?[jt]sx?$")
BACKEND_TEST = re.compile(r"backend/tests/(?:[\w/-]*/)?(?:test_[\w-]+|[\w-]+_test)\.py$")
BACKEND_SMOKE = "backend/tests/test_api_workflow.py"
WEB_SMOKE = ("fronted/tests/workbench.spec.ts", "fronted/tests/next-navigation.spec.ts")
FULL_MARKER = "Full regression checkpoint"
HIGH_RISK_TEST = re.compile(
    r"migrat|schema|immutable|concurr|permission|auth|budget|schedul|runtime|restart|"
    r"workflow|broker|dispatch|gateway|credential|session|grant|quota|storage|s3|upload|"
    r"contract|producer|render|timeline|sqlite|postgres|windows|cost"
)

# These are presentation modules with existing browser coverage. API clients,
# hooks, permission/budget controls, shared runtime/configuration, and unknown
# components deliberately remain on full regression.
WEB_SCOPES = {
    "fronted/src/components/brand-art.tsx": ("product-brand",),
    "fronted/src/components/shared/list-pagination.tsx": ("pagination",),
    "fronted/src/features/canvas/components/canvas-node-list.tsx": ("canvas-media",),
}


@dataclass(frozen=True)
class Scope:
    mode: str
    reason: str
    checkpoint: str | None = None
    tasks: tuple[str, ...] = ()
    changes: tuple[str, ...] = ()
    backend_tests: tuple[str, ...] = ()
    web_tests: tuple[str, ...] = ()


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.rstrip("\n")


def ancestor(root: Path, before: str, after: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", before, after],
        cwd=root,
        capture_output=True,
    )
    return result.returncode == 0


def changes(root: Path, before: str, after: str) -> dict[str, str]:
    fields = git(root, "diff", "--name-status", "--no-renames", "-z", before, after).split("\0")
    return dict(zip(fields[1::2], fields[::2], strict=False))


def document(path: str) -> bool:
    return (
        path.endswith(".md")
        and not path.startswith((".github/", ".agents/"))
        and (Path(path).name != "AGENTS.md")
    )


def selection(root: Path, changed: dict[str, str]) -> tuple[set[str], set[str], list[str]]:
    backend: set[str] = set()
    web: set[str] = set()
    unknown = []
    for path, status in changed.items():
        # Deletions and renames can remove previously tested contracts.
        if status != "D" and document(path):
            continue
        if (
            status == "D"
            or not (root / path).is_file()
            or path in {"backend/tests/test_ci_scope.py", "backend/tests/test_preflight.py"}
        ):
            unknown.append(path)
        elif BACKEND_TEST.fullmatch(path):
            if HIGH_RISK_TEST.search(Path(path).stem):
                unknown.append(path)
            else:
                backend.add(path)
        elif WEB_TEST.fullmatch(path):
            if HIGH_RISK_TEST.search(Path(path).stem):
                unknown.append(path)
            else:
                web.add(path)
        elif path in WEB_SCOPES:
            web.update(f"fronted/tests/{name}.spec.ts" for name in WEB_SCOPES[path])
        elif path.startswith("fronted/src/components/ui/") and path.endswith((".tsx", ".css")):
            web.add("fronted/tests/ui-components.spec.ts")
        else:
            unknown.append(path)
    if backend:
        backend.add(BACKEND_SMOKE)
    if web:
        web.update(WEB_SMOKE)
    unknown.extend(path for path in (*backend, *web) if not (root / path).is_file())
    return backend, web, unknown


def request_json(path: str, token: str) -> dict:
    request = Request(
        "https://api.github.com/" + path,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urlopen(request, timeout=20) as response:
        data = json.load(response)
    if not isinstance(data, dict):
        raise ValueError("Invalid Actions response")
    return data


def full_run(jobs: list[dict]) -> bool:
    marker = next((job for job in jobs if job["name"] == FULL_MARKER), None)
    if marker and marker["conclusion"] != "skipped":
        return True
    database = [
        job
        for job in jobs
        if re.fullmatch(r"check \((?:sqlite, [12]/2|postgres, [1-4]/4)\)", job["name"])
    ]
    browser = [job for job in jobs if job["name"] in {"web (1/2)", "web (2/2)"}]
    # Compatibility with the existing full-only workflow; skipped matrix jobs
    # from the new workflow do not constitute a full regression attempt.
    return any(job["conclusion"] != "skipped" for job in (*database, *browser))


def successful_full(jobs: list[dict]) -> bool:
    marker = next((job for job in jobs if job["name"] == FULL_MARKER), None)
    if marker:
        return marker["conclusion"] == "success"
    expected = {
        "check (sqlite, 1/2)",
        "check (sqlite, 2/2)",
        *(f"check (postgres, {index}/4)" for index in range(1, 5)),
        "web (1/2)",
        "web (2/2)",
        "Arclight CI passed",
    }
    successful = {job["name"] for job in jobs if job["conclusion"] == "success"}
    return expected <= successful


def checkpoint(root: Path, head: str) -> tuple[str | None, bool]:
    """Find a successful ancestor full run; remember newer failed/full attempts."""
    token = os.environ.get("GITHUB_TOKEN", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    branch = os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME", "")
    if not token or not re.fullmatch(r"[\w.-]+/[\w.-]+", repository) or not branch:
        return None, False
    query = urlencode({"branch": branch, "per_page": 100})
    runs = request_json(f"repos/{repository}/actions/workflows/ci.yml/runs?{query}", token)
    failed_full = False
    current = os.environ.get("GITHUB_RUN_ID")
    for run in runs["workflow_runs"]:
        if str(run["id"]) == current or run["event"] not in {"push", "workflow_dispatch"}:
            continue
        if not ancestor(root, run["head_sha"], head):
            continue
        payload = request_json(
            f"repos/{repository}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100",
            token,
        )
        jobs = payload["jobs"]
        if payload["total_count"] > len(jobs):
            raise ValueError("Incomplete checkpoint job list")
        if run["conclusion"] == "success" and successful_full(jobs):
            return run["head_sha"], failed_full
        if run["conclusion"] != "success" or full_run(jobs):
            # An incomplete/failed selector can leave every matrix skipped. Its
            # intended scope is unknown, so only a successful full run resets it.
            failed_full = True
    return None, failed_full


def plan(
    root: Path,
    base: str,
    head: str,
    full_base: str | None,
    *,
    force_full: bool = False,
    failed_full: bool = False,
) -> Scope:
    if force_full:
        return Scope("full", "Explicit full checkpoint requested", full_base)
    if not full_base or not ancestor(root, full_base, head):
        return Scope("full", "No verified successful ancestor full checkpoint")
    if not ancestor(root, base, head) or not ancestor(root, full_base, base):
        return Scope("full", "Change base does not follow the verified checkpoint", full_base)
    changed = changes(root, full_base, head)
    paths = tuple(sorted(changed))
    if failed_full:
        return Scope("full", "A newer full regression did not complete successfully", full_base)
    task_ids: set[str] = set()
    for commit in git(root, "rev-list", "--reverse", f"{full_base}..{head}").splitlines():
        message = git(root, "show", "-s", "--format=%B", commit)
        if FULL.search(message):
            return Scope("full", "Commit requests a full checkpoint", full_base, changes=paths)
        parents = git(root, "show", "-s", "--format=%P", commit).split()
        if len(parents) != 1:
            return Scope("full", "Merge or root commit requires full regression", full_base)
        commit_changes = changes(root, parents[0], commit)
        _, _, unknown = selection(root, commit_changes)
        if unknown:
            return Scope(
                "full", f"High-risk or unknown scope: {unknown[0]}", full_base, changes=paths
            )
        identities = TASK.findall(message)
        if any(not document(path) for path in commit_changes) and len(identities) != 1:
            return Scope(
                "full", "Missing or ambiguous Arclight-Task trailer", full_base, changes=paths
            )
        task_ids.update(identities)
    tasks = tuple(sorted(task_ids))
    if len(tasks) >= 3:
        return Scope(
            "full", "Third distinct task since the full checkpoint", full_base, tasks, paths
        )
    backend, web, unknown = selection(root, changed)
    if unknown:
        return Scope("full", f"High-risk or unknown scope: {unknown[0]}", full_base, tasks, paths)
    return Scope(
        "fast",
        "Known changes and smoke tests since the verified full checkpoint",
        full_base,
        tasks,
        paths,
        tuple(sorted(backend)),
        tuple(sorted(web)),
    )


def verify_report(package: str, report: Path, selected: list[str]) -> None:
    """Fast gates require real first-attempt passes for every selected file."""
    if not selected:
        raise ValueError("No selected test files")
    observed: set[str] = set()
    if package == "backend":
        cases = list(ElementTree.parse(report).getroot().iter("testcase"))
        for case in cases:
            if any(case.find(tag) is not None for tag in ("failure", "error", "skipped")):
                raise ValueError("Fast backend verification has failure, error, or unresolved skip")
            identity = case.get("classname", "").replace(".", "/")
            for path in selected:
                stem = path.removesuffix(".py")
                if identity == stem or identity.startswith(stem + "/"):
                    observed.add(path)
    else:

        def visit(suite: dict) -> None:
            for spec in suite.get("specs", []):
                path = spec.get("file", suite.get("file", "")).replace("\\", "/")
                for test in spec.get("tests", []):
                    results = test.get("results", [])
                    if (
                        test.get("status") != "expected"
                        or len(results) != 1
                        or results[0].get("status") != "passed"
                    ):
                        raise ValueError("Fast browser verification lacks a first-attempt pass")
                    observed.update(
                        name
                        for name in selected
                        if path == name
                        or path == name.removeprefix("tests/")
                        or path.endswith("/" + name)
                    )
            for child in suite.get("suites", []):
                visit(child)

        data = json.loads(report.read_text(encoding="utf-8"))
        if data.get("errors") or any(
            data.get("stats", {}).get(name, 0) for name in ("unexpected", "flaky", "skipped")
        ):
            raise ValueError("Browser report contains errors or unresolved skipped/retried cases")
        for suite in data.get("suites", []):
            visit(suite)
    if not set(selected) <= observed:
        raise ValueError(
            "Selected files have no actual passing cases: "
            + ", ".join(sorted(set(selected) - observed))
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base")
    parser.add_argument("--verify-report", choices=("backend", "web"))
    parser.add_argument("--report", type=Path)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--force-full", action="store_true")
    parser.add_argument("--summary", type=Path, default=ROOT / "backend/test-results/ci-scope.json")
    args = parser.parse_args(argv)
    if args.verify_report:
        try:
            selected = json.loads(os.environ.get("ARCLIGHT_CI_SELECTED_TESTS", "[]"))
            if (
                args.report is None
                or not isinstance(selected, list)
                or not all(isinstance(path, str) for path in selected)
            ):
                raise ValueError("Report and selected test files are required")
            verify_report(args.verify_report, args.report, selected)
            return 0
        except (OSError, ValueError, ElementTree.ParseError) as error:
            print(str(error))
            return 1
    if not args.base:
        parser.error("--base is required when planning CI")
    forced = args.force_full or os.environ.get("ARCLIGHT_FORCE_FULL", "").lower() == "true"
    try:
        full_base, failed = (None, False) if forced else checkpoint(ROOT, args.head)
        scope = plan(ROOT, args.base, args.head, full_base, force_full=forced, failed_full=failed)
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        scope = Scope("full", f"Checkpoint evidence unavailable ({type(error).__name__})")
    data = asdict(scope) | {"base": args.base, "head": args.head, "task_count": len(scope.tasks)}
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(data, ensure_ascii=False, indent=2))
    outputs = {
        "mode": scope.mode,
        "backend_matrix": json.dumps(
            {
                "include": [
                    {"database": database, "shard": shard, "shards": shards}
                    for database, shards in (("sqlite", 2), ("postgres", 4))
                    for shard in range(1, shards + 1)
                ]
                if scope.mode == "full"
                else [{"database": "postgres", "shard": 1, "shards": 1}]
            }
        ),
        "web_matrix": json.dumps(
            {
                "include": [
                    {"shard": shard, "shards": 2 if scope.mode == "full" else 1}
                    for shard in range(1, 3 if scope.mode == "full" else 2)
                ]
            }
        ),
        "backend": str(bool(scope.backend_tests)).lower(),
        "web": str(bool(scope.web_tests)).lower(),
        "backend_tests": json.dumps(
            [path.removeprefix("backend/") for path in scope.backend_tests]
        ),
        "web_tests": json.dumps([path.removeprefix("fronted/") for path in scope.web_tests]),
    }
    if path := os.environ.get("GITHUB_OUTPUT"):
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.writelines(f"{key}={value}\n" for key, value in outputs.items())
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(f"CI scope: **{scope.mode}** — {scope.reason}\n\n")
            stream.write(
                f"Tasks after full checkpoint: {len(scope.tasks)}; "
                f"checkpoint: `{scope.checkpoint}`\n"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
