"""Tier selection, checkpoint evidence, and task counting without running services."""

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "ci_scope", Path(__file__).resolve().parents[1] / "scripts" / "ci_scope.py"
)
assert spec is not None and spec.loader is not None
ci_scope = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ci_scope
spec.loader.exec_module(ci_scope)


def write(root, path, content="initial\n"):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def commit(root, task="UI-01", message="Update presentation", full=False):
    ci_scope.git(root, "add", ".")
    body = message + (f"\n\nArclight-Task: {task}" if task else "")
    if full:
        body += "\nArclight-CI: full"
    ci_scope.git(root, "commit", "--allow-empty", "-m", body)
    return ci_scope.git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    ci_scope.git(tmp_path, "init", "-b", "main")
    ci_scope.git(tmp_path, "config", "user.name", "CI scope test")
    ci_scope.git(tmp_path, "config", "user.email", "ci-scope@example.invalid")
    for path in (
        ci_scope.BACKEND_SMOKE,
        *ci_scope.WEB_SMOKE,
        "fronted/tests/product-brand.spec.ts",
        "fronted/tests/ui-components.spec.ts",
        "fronted/src/components/brand-art.tsx",
    ):
        write(tmp_path, path)
    commit(tmp_path, full=True)
    return tmp_path


def test_known_ui_scope_includes_old_smoke_paths(repo):
    base = ci_scope.git(repo, "rev-parse", "HEAD")
    write(repo, "fronted/src/components/brand-art.tsx", "updated\n")
    head = commit(repo)
    plan = ci_scope.plan(repo, base, head, base)
    assert plan.mode == "fast"
    assert plan.tasks == ("UI-01",)
    assert set(plan.web_tests) == {*ci_scope.WEB_SMOKE, "fronted/tests/product-brand.spec.ts"}
    assert plan.backend_tests == ()


def test_changed_backend_and_browser_tests_are_always_selected(repo):
    base = ci_scope.git(repo, "rev-parse", "HEAD")
    write(repo, "backend/tests/test_example.py")
    write(repo, "fronted/tests/example.spec.ts")
    head = commit(repo)
    plan = ci_scope.plan(repo, base, head, base)
    assert plan.mode == "fast"
    assert set(plan.backend_tests) == {ci_scope.BACKEND_SMOKE, "backend/tests/test_example.py"}
    assert set(plan.web_tests) == {*ci_scope.WEB_SMOKE, "fronted/tests/example.spec.ts"}


@pytest.mark.parametrize(
    "path",
    [
        "backend/src/arclight/domain/permissions.py",
        "backend/src/arclight/application/generation_budget.py",
        "backend/src/arclight/application/runtime.py",
        "backend/migrations/versions/0070_example.py",
        "backend/tests/conftest.py",
        "backend/tests/test_ci_scope.py",
        "backend/tests/test_preflight.py",
        "backend/scripts/preflight.py",
        "backend/uv.lock",
        "fronted/package-lock.json",
        "fronted/src/features/auth/components/login.tsx",
        "fronted/src/features/projects/hooks/use-production-list.ts",
        "fronted/src/features/projects/components/project-costs.tsx",
        "fronted/src/features/projects/components/project-overview.tsx",
        "fronted/src/features/editor/components/shot-viewer.tsx",
        "fronted/src/features/episodes/components/production-stage-navigation.tsx",
        "fronted/src/features/assets/components/version-image.tsx",
        "fronted/src/features/canvas/components/media-details.tsx",
        "fronted/src/features/rendering/components/delivery-preview.tsx",
        ".github/workflows/ci.yml",
        "unknown.txt",
    ],
)
def test_high_risk_and_unknown_changes_force_full(repo, path):
    base = ci_scope.git(repo, "rev-parse", "HEAD")
    write(repo, path)
    head = commit(repo)
    assert ci_scope.plan(repo, base, head, base).mode == "full"


def test_third_distinct_task_triggers_full_but_same_task_fixes_do_not(repo):
    checkpoint = ci_scope.git(repo, "rev-parse", "HEAD")
    for index, task in enumerate(("UI-01", "UI-01", "UI-02")):
        write(repo, "fronted/tests/example.spec.ts", str(index))
        head = commit(repo, task)
        assert ci_scope.plan(repo, checkpoint, head, checkpoint).mode == "fast"
    write(repo, "fronted/tests/example.spec.ts", "third task")
    head = commit(repo, "UI-03")
    plan = ci_scope.plan(repo, checkpoint, head, checkpoint)
    assert plan.mode == "full"
    assert plan.tasks == ("UI-01", "UI-02", "UI-03")


def test_scope_keeps_earlier_task_changes_when_push_base_is_newer(repo):
    checkpoint = ci_scope.git(repo, "rev-parse", "HEAD")
    write(repo, "backend/tests/test_previous.py")
    base = commit(repo, "TEST-01")
    write(repo, "fronted/tests/current.spec.ts")
    head = commit(repo, "UI-02")
    plan = ci_scope.plan(repo, base, head, checkpoint)
    assert plan.mode == "fast"
    assert "backend/tests/test_previous.py" in plan.backend_tests
    assert "fronted/tests/current.spec.ts" in plan.web_tests


def test_reverted_high_risk_change_still_requires_full(repo):
    checkpoint = ci_scope.git(repo, "rev-parse", "HEAD")
    path = "backend/src/arclight/application/runtime.py"
    write(repo, path)
    commit(repo, "PROD-01")
    (repo / path).unlink()
    head = commit(repo, "PROD-01")
    assert ci_scope.plan(repo, checkpoint, head, checkpoint).mode == "full"


@pytest.mark.parametrize("task", [None, "invalid trailer", "UI-01\nArclight-Task: UI-02"])
def test_missing_or_ambiguous_task_identity_cannot_enter_fast_mode(repo, task):
    base = ci_scope.git(repo, "rev-parse", "HEAD")
    write(repo, "fronted/tests/example.spec.ts")
    head = commit(repo, task)
    assert ci_scope.plan(repo, base, head, base).mode == "full"


def test_doc_only_changes_do_not_start_business_services(repo):
    base = ci_scope.git(repo, "rev-parse", "HEAD")
    write(repo, "README.md")
    head = commit(repo, task=None)
    plan = ci_scope.plan(repo, base, head, base)
    assert plan.mode == "fast"
    assert not plan.backend_tests and not plan.web_tests


def test_failed_full_and_explicit_checkpoint_never_downgrade(repo):
    base = ci_scope.git(repo, "rev-parse", "HEAD")
    write(repo, "fronted/tests/example.spec.ts")
    head = commit(repo)
    assert ci_scope.plan(repo, base, head, base, failed_full=True).mode == "full"
    assert ci_scope.plan(repo, base, head, base, force_full=True).mode == "full"
    head = commit(repo, full=True)
    assert ci_scope.plan(repo, base, head, base).mode == "full"


def test_missing_checkpoint_and_deleted_tests_force_full(repo):
    base = ci_scope.git(repo, "rev-parse", "HEAD")
    (repo / ci_scope.WEB_SMOKE[0]).unlink()
    head = commit(repo)
    assert ci_scope.plan(repo, base, head, base).mode == "full"
    assert ci_scope.plan(repo, base, head, None).mode == "full"


def test_checkpoint_must_be_ancestor_and_base_cannot_hide_changes(repo):
    base = ci_scope.git(repo, "rev-parse", "HEAD")
    write(repo, "README.md")
    head = commit(repo)
    assert ci_scope.plan(repo, base, base, head).mode == "full"
    assert ci_scope.plan(repo, base, head, head).mode == "full"


def jobs(marker="success"):
    return [{"name": ci_scope.FULL_MARKER, "conclusion": marker}]


def test_checkpoint_requires_successful_full_run_and_remembers_newer_failure(repo, monkeypatch):
    base = ci_scope.git(repo, "rev-parse", "HEAD")
    monkeypatch.setenv("GITHUB_TOKEN", "test-only")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/project")
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    monkeypatch.setenv("GITHUB_RUN_ID", "3")
    runs = [
        {"id": 3, "event": "push", "head_sha": base, "conclusion": None},
        {"id": 2, "event": "push", "head_sha": base, "conclusion": "failure"},
        {"id": 1, "event": "push", "head_sha": base, "conclusion": "success"},
    ]

    def response(path, token):
        assert token == "test-only"
        if "workflows/" in path:
            return {"workflow_runs": runs}
        return {"total_count": 1, "jobs": jobs("failure" if "/2/" in path else "success")}

    monkeypatch.setattr(ci_scope, "request_json", response)
    assert ci_scope.checkpoint(repo, base) == (base, True)


def test_skipped_full_marker_does_not_count_as_checkpoint():
    assert not ci_scope.full_run(jobs("skipped"))
    assert not ci_scope.successful_full(jobs("skipped"))
    assert ci_scope.full_run(jobs("failure"))
    assert not ci_scope.successful_full(jobs("failure"))


def test_legacy_checkpoint_requires_all_existing_shards():
    names = [
        "check (sqlite, 1/2)",
        "check (sqlite, 2/2)",
        *(f"check (postgres, {index}/4)" for index in range(1, 5)),
        "web (1/2)",
        "web (2/2)",
        "Arclight CI passed",
    ]
    legacy = [{"name": name, "conclusion": "success"} for name in names]
    assert ci_scope.successful_full(legacy)
    assert not ci_scope.successful_full(legacy[:-1])
    legacy[0]["conclusion"] = "skipped"
    assert not ci_scope.successful_full(legacy)


def test_api_failure_falls_back_to_full_and_writes_summary(repo, monkeypatch):
    output = repo / "outputs"
    summary = repo / "summary.json"
    monkeypatch.setattr(ci_scope, "ROOT", repo)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    def unavailable(*args):
        raise OSError("API unavailable")

    monkeypatch.setattr(ci_scope, "checkpoint", unavailable)
    assert ci_scope.main(["--base", "HEAD", "--summary", str(summary)]) == 0
    assert json.loads(summary.read_text())["mode"] == "full"
    assert "mode=full\n" in output.read_text()
    assert "backend=false\n" in output.read_text()


def test_dispatch_does_not_need_checkpoint_api(repo, monkeypatch):
    monkeypatch.setattr(ci_scope, "ROOT", repo)
    monkeypatch.setenv("ARCLIGHT_FORCE_FULL", "true")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(ci_scope, "checkpoint", lambda *args: pytest.fail("API was unnecessary"))
    summary = repo / "summary.json"
    assert ci_scope.main(["--base", "HEAD", "--summary", str(summary)]) == 0
    assert json.loads(summary.read_text())["mode"] == "full"


def test_script_is_executable_without_project_dependencies(repo):
    script = Path(ci_scope.__file__)
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--base",
            "HEAD",
            "--force-full",
            "--summary",
            str(repo / "scope.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["mode"] == "full"


@pytest.mark.parametrize(
    "name",
    [
        "test_permissions.py",
        "test_generation_budget.py",
        "test_run_concurrency.py",
        "auth.spec.ts",
        "project-costs.spec.ts",
    ],
)
def test_changed_high_risk_tests_alone_force_full(tmp_path, name):
    prefix = "backend/tests/" if name.endswith(".py") else "fronted/tests/"
    path = prefix + name
    write(tmp_path, path)
    _, _, unknown = ci_scope.selection(tmp_path, {path: "M"})
    assert path in unknown


def test_successful_quick_run_does_not_look_like_failed_full_checkpoint():
    quick = jobs("skipped") + [
        {"name": "check (postgres, 1/1)", "conclusion": "success"},
        {"name": "web (1/1)", "conclusion": "success"},
    ]
    assert not ci_scope.full_run(quick)
    assert not ci_scope.successful_full(quick)


@pytest.mark.parametrize(
    "body",
    [
        "",
        '<testcase classname="tests.test_example" name="test_a"><skipped/></testcase>',
        '<testcase classname="tests.test_example" name="test_a"><failure/></testcase>',
        '<testcase classname="tests.test_other" name="test_a"/>',
    ],
)
def test_fast_backend_report_rejects_empty_skipped_failed_or_wrong_cases(tmp_path, body):
    report = tmp_path / "pytest.xml"
    report.write_text("<testsuite>" + body + "</testsuite>", encoding="utf-8")
    with pytest.raises(ValueError):
        ci_scope.verify_report("backend", report, ["tests/test_example.py"])


def test_fast_backend_report_requires_each_selected_file(tmp_path):
    report = tmp_path / "pytest.xml"
    report.write_text(
        '<testsuite><testcase classname="tests.test_example" name="test_a"/></testsuite>',
        encoding="utf-8",
    )
    ci_scope.verify_report("backend", report, ["tests/test_example.py"])
    with pytest.raises(ValueError, match="Selected files"):
        ci_scope.verify_report(
            "backend", report, ["tests/test_example.py", "tests/test_missing.py"]
        )


@pytest.mark.parametrize(
    "status,results",
    [
        ("skipped", []),
        ("flaky", [{"status": "failed"}, {"status": "passed"}]),
        ("expected", []),
        ("unexpected", [{"status": "failed"}]),
    ],
)
def test_fast_browser_report_rejects_unexecuted_skips_failures_and_retries(
    tmp_path, status, results
):
    report = tmp_path / "playwright.json"
    report.write_text(
        json.dumps(
            {
                "suites": [
                    {
                        "file": "tests/example.spec.ts",
                        "specs": [{"tests": [{"status": status, "results": results}]}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        ci_scope.verify_report("web", report, ["tests/example.spec.ts"])


def test_fast_browser_report_requires_actual_selected_cases(tmp_path):
    report = tmp_path / "playwright.json"
    data = {
        "suites": [
            {
                "file": "tests/example.spec.ts",
                "specs": [{"tests": [{"status": "expected", "results": [{"status": "passed"}]}]}],
            }
        ]
    }
    report.write_text(json.dumps(data), encoding="utf-8")
    ci_scope.verify_report("web", report, ["tests/example.spec.ts"])
    with pytest.raises(ValueError, match="Selected files"):
        ci_scope.verify_report("web", report, ["tests/missing.spec.ts"])


def test_full_matrices_preserve_all_original_shards(repo, monkeypatch):
    output = repo / "outputs"
    monkeypatch.setattr(ci_scope, "ROOT", repo)
    monkeypatch.setenv("ARCLIGHT_FORCE_FULL", "true")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    ci_scope.main(["--base", "HEAD", "--summary", str(repo / "scope.json")])
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    backend = json.loads(values["backend_matrix"])["include"]
    assert [(item["database"], item["shard"], item["shards"]) for item in backend] == [
        ("sqlite", 1, 2),
        ("sqlite", 2, 2),
        ("postgres", 1, 4),
        ("postgres", 2, 4),
        ("postgres", 3, 4),
        ("postgres", 4, 4),
    ]
    assert json.loads(values["web_matrix"])["include"] == [
        {"shard": 1, "shards": 2},
        {"shard": 2, "shards": 2},
    ]


@pytest.mark.parametrize(
    "unknown_jobs",
    [
        [],
        [
            {"name": "Select verification scope", "conclusion": "failure"},
            {"name": ci_scope.FULL_MARKER, "conclusion": "skipped"},
        ],
    ],
)
def test_unsuccessful_unknown_scope_requires_full_even_when_all_jobs_skipped(
    repo, monkeypatch, unknown_jobs
):
    base = ci_scope.git(repo, "rev-parse", "HEAD")
    monkeypatch.setenv("GITHUB_TOKEN", "test-only")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/project")
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    monkeypatch.setenv("GITHUB_RUN_ID", "3")
    runs = [
        {"id": 2, "event": "push", "head_sha": base, "conclusion": "failure"},
        {"id": 1, "event": "push", "head_sha": base, "conclusion": "success"},
    ]

    def response(path, token):
        if "workflows/" in path:
            return {"workflow_runs": runs}
        selected = unknown_jobs if "/2/" in path else jobs()
        return {"total_count": len(selected), "jobs": selected}

    monkeypatch.setattr(ci_scope, "request_json", response)
    assert ci_scope.checkpoint(repo, base) == (base, True)


def test_browser_report_accepts_playwright_paths_relative_to_test_directory(tmp_path):
    report = tmp_path / "playwright.json"
    report.write_text(
        json.dumps(
            {
                "suites": [
                    {
                        "file": "example.spec.ts",
                        "specs": [
                            {"tests": [{"status": "expected", "results": [{"status": "passed"}]}]}
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    ci_scope.verify_report("web", report, ["tests/example.spec.ts"])


def test_workflow_plans_and_tests_the_same_exact_commit():
    workflow = Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"
    text = workflow.read_text(encoding="utf-8")
    checkouts = re.findall(r"- uses: actions/checkout@v6\n(.*?)(?=      -|\Z)", text, re.DOTALL)
    assert len(checkouts) == 3
    assert all(
        "ref: ${{ github.event.pull_request.head.sha || github.sha }}" in checkout
        for checkout in checkouts
    )
