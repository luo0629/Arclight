"""Preflight selection and result gates without starting services or external tools."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "preflight", Path(__file__).resolve().parents[1] / "scripts" / "preflight.py"
)
assert spec is not None and spec.loader is not None
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


def write(root, name, content=""):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path):
    write(tmp_path, ".gitignore", "backend/test-results/\n")
    write(tmp_path, "backend/src/example.py", "VALUE = 1\n")
    write(tmp_path, "backend/tests/test_existing.py", "def test_existing(): pass\n")
    write(tmp_path, "fronted/tests/existing.spec.ts", "// existing\n")
    for args in (
        ("init",),
        ("add", "."),
        (
            "-c",
            "user.name=Preflight",
            "-c",
            "user.email=preflight@example.invalid",
            "commit",
            "-m",
            "baseline",
        ),
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def args(*extra):
    return preflight.parser().parse_args(["--base", "HEAD", *extra])


def summary(repo):
    files = list((repo / "backend/test-results/preflight").glob("*/summary.json"))
    return json.loads(files[-1].read_text(encoding="utf-8"))


def test_changed_and_untracked_whole_test_files_are_always_selected(repo):
    write(repo, "backend/tests/test_existing.py", "def test_existing(): assert True\n")
    write(repo, "backend/tests/test_new.py", "def test_new(): pass\n")
    write(repo, "fronted/tests/new.spec.ts", "// new\n")
    plan = preflight.make_plan(repo, args())
    assert plan.backend == ("backend/tests/test_existing.py", "backend/tests/test_new.py")
    assert plan.web == ("fronted/tests/new.spec.ts",)
    assert len(plan.base) == 40
    assert plan.fingerprints["backend/tests/test_new.py"]


def test_ci_alternate_test_filenames_are_selected_and_accepted_explicitly(repo):
    write(repo, "backend/tests/alternate_test.py", "def test_new(): pass\n")
    write(repo, "fronted/tests/alternate.test.ts", "// new\n")
    plan = preflight.make_plan(
        repo,
        args("--backend-test", "tests/alternate_test.py", "--web-test", "tests/alternate.test.ts"),
    )
    assert plan.backend == ("backend/tests/alternate_test.py",)
    assert plan.web == ("fronted/tests/alternate.test.ts",)


def test_migration_change_adds_older_migration_and_history_modules(repo):
    names = (
        "test_migrations.py",
        "test_media_size_migrations.py",
        "legacy_migration_test.py",
        "test_immutable_history.py",
        "test_content_history.py",
    )
    for name in names:
        write(repo, "backend/tests/" + name, "def test_guard(): pass\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Preflight",
            "-c",
            "user.email=preflight@example.invalid",
            "commit",
            "-m",
            "historical migration tests",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    write(repo, "backend/migrations/versions/next_revision.py", "revision = 'next'\n")
    plan = preflight.make_plan(repo, args())
    assert plan.risks == ("migration",)
    assert set(plan.backend) == {"backend/tests/" + name for name in names}


def test_deleted_test_is_not_claimed_as_executed(repo):
    (repo / "backend/tests/test_existing.py").unlink()
    plan = preflight.make_plan(repo, args())
    assert not plan.backend
    assert any("Deleted tests" in issue for issue in plan.issues)


@pytest.mark.parametrize(
    "selection",
    [
        "tests/test_existing.py::test_existing",
        "tests/*.py",
        "tests",
        "tests/missing.py",
        "../backend/tests/test_existing.py",
        "tests/test_existing.py -k guard",
    ],
)
def test_partial_missing_or_escaping_selection_is_rejected(repo, selection):
    with pytest.raises(ValueError):
        preflight.make_plan(repo, args("--backend-test", selection))


@pytest.mark.parametrize("base", ["not-a-commit", "", "--help"])
def test_invalid_base_is_rejected_before_diff(repo, base):
    options = args()
    options.base = base
    with pytest.raises(ValueError):
        preflight.make_plan(repo, options)


def test_empty_scope_and_unmapped_code_change_cannot_pass(repo, monkeypatch):
    write(repo, "backend/src/example.py", "VALUE = 2\n")
    monkeypatch.setattr(preflight, "run_command", lambda *a: pytest.fail("Must not execute"))
    assert preflight.main(["--base", "HEAD", "--run"], root=repo) == 2
    result = summary(repo)
    assert result["status"] == "incomplete"
    assert any("--backend-test" in issue for issue in result["issues"])
    assert result["commands"] == []


def test_plan_is_not_execution_evidence(repo, monkeypatch):
    monkeypatch.setattr(preflight, "run_command", lambda *a: pytest.fail("Plan cannot execute"))
    assert (
        preflight.main(["--base", "HEAD", "--backend-test", "tests/test_existing.py"], root=repo)
        == 0
    )
    assert summary(repo)["status"] == "planned"
    assert summary(repo)["commands"] == []


def test_missing_required_service_stops_before_commands(repo, monkeypatch):
    monkeypatch.delenv("ARCLIGHT_TEST_DATABASE_URL", raising=False)
    monkeypatch.setattr(preflight, "run_command", lambda *a: pytest.fail("Missing dependency"))
    assert (
        preflight.main(
            [
                "--base",
                "HEAD",
                "--backend-test",
                "tests/test_existing.py",
                "--risk",
                "concurrency",
                "--run",
            ],
            root=repo,
        )
        == 2
    )
    assert "ARCLIGHT_TEST_DATABASE_URL" in " ".join(summary(repo)["issues"])


@pytest.mark.parametrize(
    "variable,value,risk",
    [
        ("ARCLIGHT_TEST_DATABASE_URL", "postgresql+psycopg://u:p@localhost/business", "migration"),
        ("ARCLIGHT_TEST_REDIS_URL", "redis://production.example/15", "broker"),
        ("ARCLIGHT_TEST_REDIS_URL", "redis://localhost/0", "broker"),
    ],
)
def test_unsafe_database_targets_are_rejected_without_exposing_address(
    repo, monkeypatch, variable, value, risk
):
    monkeypatch.setenv(variable, value)
    plan = preflight.make_plan(repo, args("--risk", risk))
    environment, issues = preflight.test_environment(plan)
    assert issues
    assert variable not in environment
    assert value not in " ".join(issues)


def test_environment_removes_business_and_live_switches(repo, monkeypatch):
    for name in (
        "ARCLIGHT_DATABASE_URL",
        "ARCLIGHT_TEST_S3_ENDPOINT",
        "ARCLIGHT_AUDIO_LIVE",
        "ARCLIGHT_LLM_API_KEY",
        "ARCLIGHT_E2E_BASE_URL",
        "ARCLIGHT_E2E_S3_ENDPOINT",
    ):
        monkeypatch.setenv(name, "sensitive-value")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--collect-only -k hidden")
    monkeypatch.setenv("PLAYWRIGHT_JSON_OUTPUT_FILE", "old-report.json")
    environment, issues = preflight.test_environment(preflight.make_plan(repo, args()))
    assert not issues
    assert environment["PYTEST_ADDOPTS"] == ""
    assert environment["ARCLIGHT_PROVIDER"] == "mock"
    assert "sensitive-value" not in environment.values()
    assert "PLAYWRIGHT_JSON_OUTPUT_FILE" not in environment


def junit(repo, name, body):
    return write(repo, name, f"<testsuites><testsuite>{body}</testsuite></testsuites>")


def test_postgres_pass_can_supply_sqlite_skip_evidence(repo):
    case = '<testcase classname="tests.test_existing" name="test_pg">'
    sqlite = junit(repo, "sqlite.xml", case + '<skipped message="Requires PG"/></testcase>')
    postgres = junit(repo, "postgres.xml", case + "</testcase>")
    result = preflight.junit_evidence([sqlite, postgres], ("backend/tests/test_existing.py",))
    assert result.passed == {"tests.test_existing::test_pg"}
    assert not result.skipped and not result.errors


def test_unresolved_skip_retains_reason(repo):
    path = junit(
        repo,
        "sqlite.xml",
        '<testcase classname="tests.test_existing" name="test_s3">'
        '<skipped message="Requires dedicated S3"/></testcase>',
    )
    result = preflight.junit_evidence([path], ("backend/tests/test_existing.py",))
    assert result.skipped == {"tests.test_existing::test_s3": "Requires dedicated S3"}


@pytest.mark.parametrize(
    "body",
    [
        "",
        '<testcase classname="tests.test_other" name="test_other"/>',
        '<testcase classname="tests.test_existing" name="test_failure"><failure/></testcase>',
    ],
)
def test_empty_collection_missing_selected_module_and_failures_cannot_pass(repo, body):
    path = junit(repo, "pytest.xml", body)
    result = preflight.junit_evidence([path], ("backend/tests/test_existing.py",))
    assert result.errors or result.missing


def browser_report(repo, status="expected", results=None):
    data = {
        "suites": [
            {
                "file": "tests/existing.spec.ts",
                "specs": [
                    {
                        "title": "existing",
                        "file": "tests/existing.spec.ts",
                        "tests": [
                            {
                                "projectName": "chromium",
                                "status": status,
                                "results": [{"status": "passed"}] if results is None else results,
                                "annotations": [
                                    {"type": "skip", "description": "Platform restriction"}
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }
    return write(repo, "playwright.json", json.dumps(data))


@pytest.mark.parametrize(
    "status,results",
    [
        ("flaky", [{"status": "failed"}, {"status": "passed"}]),
        ("unexpected", [{"status": "timedOut"}]),
        ("expected", []),
    ],
)
def test_browser_retry_failure_or_collection_cannot_pass(repo, status, results):
    path = browser_report(repo, status, results)
    result = preflight.browser_evidence(path, ("fronted/tests/existing.spec.ts",))
    assert result.errors or result.missing


def test_browser_skips_are_incomplete_and_last_run_file_is_not_evidence(repo):
    path = browser_report(repo, "skipped", [{"status": "skipped"}])
    assert preflight.browser_evidence(path, ("fronted/tests/existing.spec.ts",)).skipped
    path.write_text('{"status":"passed","failedTests":[]}', encoding="utf-8")
    result = preflight.browser_evidence(path, ("fronted/tests/existing.spec.ts",))
    assert result.errors or result.missing


def test_source_drift_downgrades_success_to_incomplete(repo, monkeypatch):
    def execute(plan, folder, environment):
        write(repo, "backend/src/example.py", "VALUE = 3\n")
        return "passed", [], [], {"passed": 1, "skipped": {}}

    monkeypatch.setattr(preflight, "execute", execute)
    assert (
        preflight.main(
            ["--base", "HEAD", "--backend-test", "tests/test_existing.py", "--run"], root=repo
        )
        == 2
    )
    assert any("Source changed" in issue for issue in summary(repo)["issues"])


def test_execution_uses_whole_files_isolated_env_and_fresh_reports(repo, monkeypatch):
    calls = []
    monkeypatch.setenv(
        "ARCLIGHT_TEST_DATABASE_URL", "postgresql+psycopg://test:test@localhost/arclight_test"
    )
    monkeypatch.setenv("ARCLIGHT_E2E_BASE_URL", "https://must-not-be-used.invalid")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--collect-only")

    def run(command, cwd, environment, log):
        calls.append((command, cwd, environment))
        if "pytest" in command:
            report = next(
                arg.removeprefix("--junitxml=") for arg in command if arg.startswith("--junitxml=")
            )
            junit(repo, report, '<testcase classname="tests.test_existing" name="test_existing"/>')
        if "test:e2e" in command:
            target = Path(environment["PLAYWRIGHT_JSON_OUTPUT_FILE"])
            target.write_text(browser_report(repo).read_text(encoding="utf-8"), encoding="utf-8")
        return 0

    monkeypatch.setattr(preflight, "run_command", run)
    command = [
        "--base",
        "HEAD",
        "--backend-test",
        "tests/test_existing.py",
        "--web-test",
        "tests/existing.spec.ts",
        "--risk",
        "concurrency",
        "--run",
    ]
    assert preflight.main(command, root=repo) == 0
    result = summary(repo)
    assert result["status"] == "passed"
    pytest_calls = [(argv, env) for argv, _, env in calls if "pytest" in argv]
    assert len(pytest_calls) == 2
    assert pytest_calls[0][1]["ARCLIGHT_TEST_DATABASE_URL"] == ""
    assert pytest_calls[1][1]["ARCLIGHT_TEST_DATABASE_URL"].endswith("/arclight_test")
    assert all(env["PYTEST_ADDOPTS"] == "" for _, env in pytest_calls)
    assert all(
        "--dist=loadfile" in argv and "tests/test_existing.py" in argv for argv, _ in pytest_calls
    )
    web = next((argv, env) for argv, _, env in calls if "test:e2e" in argv)
    assert web[1]["CI"] == "1" and web[1]["ARCLIGHT_E2E_PRODUCTION"] == "1"
    assert "ARCLIGHT_E2E_BASE_URL" not in web[1]
    assert "--reporter=line,json" in web[0]
    assert "--forbid-only" in web[0]
    assert all(argv[argv.index("-o") + 1] == "addopts=" for argv, _ in pytest_calls)
    assert len(result["commands"]) == 7
    before = set((repo / "backend/test-results/preflight").iterdir())
    assert preflight.main(command, root=repo) == 0
    assert len(set((repo / "backend/test-results/preflight").iterdir()) - before) == 1


@pytest.mark.parametrize("filename", ["existing.spec.ts", "tests/existing.spec.ts"])
def test_browser_report_accepts_paths_relative_to_test_directory(repo, filename):
    path = browser_report(repo)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["config"] = {"rootDir": str(repo / "fronted/tests")}
    data["suites"][0]["file"] = filename
    data["suites"][0]["specs"][0]["file"] = filename
    path.write_text(json.dumps(data), encoding="utf-8")
    result = preflight.browser_evidence(path, ("fronted/tests/existing.spec.ts",))
    assert len(result.passed) == 1
    assert not result.errors and not result.missing


@pytest.mark.parametrize("report_kind", ["missing", "empty", "skipped", "failure"])
def test_final_status_requires_real_execution_reports(repo, monkeypatch, report_kind):
    def run(command, cwd, environment, log):
        if "pytest" in command and report_kind != "missing":
            report = next(
                arg.removeprefix("--junitxml=") for arg in command if arg.startswith("--junitxml=")
            )
            body = ""
            if report_kind in {"skipped", "failure"}:
                marker = (
                    '<skipped message="Requires test service"/>'
                    if report_kind == "skipped"
                    else '<failure message="Regression"/>'
                )
                body = (
                    '<testcase classname="tests.test_existing" name="test_existing">'
                    + marker
                    + "</testcase>"
                )
            junit(repo, report, body)
        return 0

    monkeypatch.setattr(preflight, "run_command", run)
    code = preflight.main(
        ["--base", "HEAD", "--backend-test", "tests/test_existing.py", "--run"], root=repo
    )
    assert code == (1 if report_kind == "failure" else 2)
    result = summary(repo)
    assert result["status"] == ("failed" if report_kind == "failure" else "incomplete")
    assert result["issues"]


def test_command_failure_prevents_green_even_if_an_old_report_passed(repo, monkeypatch):
    monkeypatch.setattr(preflight, "run_command", lambda *a: 1)
    assert (
        preflight.main(
            ["--base", "HEAD", "--backend-test", "tests/test_existing.py", "--run"], root=repo
        )
        == 1
    )
    assert summary(repo)["status"] == "failed"


def commit(repo):
    preflight.git(repo, "add", ".")
    preflight.git(
        repo,
        "-c",
        "user.name=Preflight",
        "-c",
        "user.email=preflight@example.invalid",
        "commit",
        "-m",
        "guard fixture",
    )
    return preflight.git(repo, "rev-parse", "HEAD")


@pytest.fixture
def passed_preflight(repo, monkeypatch):
    """Exercise the normal runner with controlled command output and execution reports."""

    def run(command, cwd, environment, log):
        log.write_text("controlled successful command\n", encoding="utf-8")
        if "pytest" in command:
            report = next(
                arg.removeprefix("--junitxml=") for arg in command if arg.startswith("--junitxml=")
            )
            junit(repo, report, '<testcase classname="tests.test_existing" name="test_existing"/>')
        if "test:e2e" in command:
            Path(environment["PLAYWRIGHT_JSON_OUTPUT_FILE"]).write_text(
                browser_report(repo).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        return 0

    monkeypatch.setattr(preflight, "run_command", run)

    def complete(*extra, base="HEAD"):
        assert (
            preflight.main(
                ["--base", base, "--backend-test", "tests/test_existing.py", "--run", *extra],
                root=repo,
            )
            == 0
        )
        receipt = json.loads(
            preflight.git_path(repo, "arclight-preflight.json").read_text(encoding="utf-8")
        )
        return Path(receipt["summary"])

    return complete


def test_push_guard_rejects_plan_and_cli_without_evidence(repo, monkeypatch, capsys):
    import io

    monkeypatch.setattr(preflight.sys, "stdin", io.StringIO(""))
    assert preflight.main(["--verify-push"], root=repo) == 2
    assert "Push blocked" in capsys.readouterr().err
    assert (
        preflight.main(["--base", "HEAD", "--backend-test", "tests/test_existing.py"], root=repo)
        == 0
    )
    with pytest.raises(ValueError, match="planned"):
        preflight.verify_push(repo)


def test_push_guard_accepts_tested_files_after_commit(repo, passed_preflight):
    base = preflight.git(repo, "rev-parse", "HEAD")
    write(repo, "backend/src/example.py", "VALUE = 2\n")
    passed_preflight()
    head = commit(repo)
    preflight.verify_push(repo, f"refs/heads/main {head} refs/heads/main {base}\n")


@pytest.mark.parametrize("committed", [False, True])
def test_push_guard_rejects_source_changes_after_execution(repo, passed_preflight, committed):
    passed_preflight()
    write(repo, "backend/src/example.py", "VALUE = 3\n")
    if committed:
        commit(repo)
    with pytest.raises(ValueError, match="Source changed|Commit the tested"):
        preflight.verify_push(repo)


def test_push_guard_does_not_fall_back_after_a_later_plan_or_failure(
    repo, passed_preflight, monkeypatch
):
    passed_preflight()
    preflight.main(["--base", "HEAD", "--backend-test", "tests/test_existing.py"], root=repo)
    with pytest.raises(ValueError, match="planned"):
        preflight.verify_push(repo)
    passed_preflight()
    monkeypatch.setattr(preflight, "run_command", lambda *a: 1)
    assert (
        preflight.main(
            ["--base", "HEAD", "--backend-test", "tests/test_existing.py", "--run"], root=repo
        )
        == 1
    )
    with pytest.raises(ValueError, match="failed"):
        preflight.verify_push(repo)


@pytest.mark.parametrize("filename", ["pytest-sqlite.xml", "command-1.log", "summary.json"])
def test_push_guard_rejects_modified_execution_evidence(repo, passed_preflight, filename):
    path = passed_preflight().parent / filename
    path.write_text("modified", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        preflight.verify_push(repo)


def test_push_guard_requires_postgres_report_for_concurrency(repo, passed_preflight, monkeypatch):
    monkeypatch.setenv(
        "ARCLIGHT_TEST_DATABASE_URL", "postgresql+psycopg://test:test@localhost/arclight_test"
    )
    path = passed_preflight("--risk", "concurrency")
    (path.parent / "pytest-postgres.xml").unlink()
    with pytest.raises(OSError):
        preflight.verify_push(repo)


@pytest.mark.parametrize(
    "body",
    [
        "",
        (
            '<testcase classname="tests.test_existing" name="test_existing">'
            '<skipped message="missing S3"/></testcase>'
        ),
        (
            '<testcase classname="tests.test_existing" name="test_existing">'
            "<failure>regression</failure></testcase>"
        ),
        '<testcase classname="tests.test_other" name="test_other"/>',
    ],
)
def test_push_guard_rechecks_actual_cases_instead_of_trusting_summary(repo, passed_preflight, body):
    path = passed_preflight()
    junit(repo, str(path.parent / "pytest-sqlite.xml"), body)
    preflight.save_summary(repo, path.parent, json.loads(path.read_text(encoding="utf-8")))
    with pytest.raises(ValueError, match="failures, missing cases or unresolved skips"):
        preflight.verify_push(repo)


def test_push_guard_rechecks_browser_results_and_report_presence(repo, passed_preflight):
    path = passed_preflight("--web-test", "tests/existing.spec.ts")
    data = json.loads(path.read_text(encoding="utf-8"))
    browser = path.parent / "playwright.json"
    browser.write_text('{"suites": []}', encoding="utf-8")
    preflight.save_summary(repo, path.parent, data)
    with pytest.raises(ValueError, match="failures, missing cases or unresolved skips"):
        preflight.verify_push(repo)
    browser.unlink()
    preflight.save_summary(repo, path.parent, data)
    with pytest.raises(ValueError, match="Missing actual"):
        preflight.verify_push(repo)


def test_push_guard_rejects_using_current_commit_as_a_post_push_baseline(repo, passed_preflight):
    remote = preflight.git(repo, "rev-parse", "HEAD")
    write(repo, "backend/src/example.py", "VALUE = 2\n")
    head = commit(repo)
    passed_preflight(base=head)
    with pytest.raises(ValueError, match="base must precede the remote"):
        preflight.verify_push(repo, f"refs/heads/main {head} refs/heads/main {remote}\n")


def test_push_guard_checks_every_ref_and_allows_deletion_without_new_code(repo):
    zero = "0" * 40
    head = preflight.git(repo, "rev-parse", "HEAD")
    preflight.verify_push(repo, f"(delete) {zero} refs/heads/old {head}\n")
    with pytest.raises(ValueError, match="other refs"):
        preflight.verify_push(repo, f"refs/heads/other {'1' * 40} refs/heads/other {zero}\n")
    with pytest.raises(ValueError, match="Invalid pre-push"):
        preflight.verify_push(repo, "malformed input\n")


def test_hook_installation_is_idempotent_and_preserves_existing_hooks(repo):
    path = preflight.install_hook(repo)
    original = path.read_bytes()
    assert preflight.install_hook(repo) == path
    assert path.read_bytes() == original
    path.write_text("#!/bin/sh\nexit 17\n", encoding="utf-8")
    with pytest.raises(ValueError, match="without overwriting"):
        preflight.install_hook(repo)
    assert path.read_text(encoding="utf-8") == "#!/bin/sh\nexit 17\n"


def test_real_git_push_blocks_plan_then_accepts_matching_executed_reports(repo, passed_preflight):
    write(
        repo, "backend/scripts/preflight.py", Path(preflight.__file__).read_text(encoding="utf-8")
    )
    base = commit(repo)
    remote = repo.parent / (repo.name + "-remote.git")
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    preflight.git(repo, "remote", "add", "guard-test", str(remote))
    preflight.git(repo, "push", "guard-test", "HEAD:refs/heads/main")
    write(repo, "backend/src/example.py", "VALUE = 2\n")
    head = commit(repo)
    preflight.main(["--base", base, "--backend-test", "tests/test_existing.py"], root=repo)
    preflight.install_hook(repo)
    blocked = subprocess.run(
        ["git", "push", "guard-test", "HEAD:refs/heads/main"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert blocked.returncode != 0
    assert "planned" in blocked.stderr
    assert preflight.git(repo, "ls-remote", "guard-test", "refs/heads/main").split()[0] == base
    passed_preflight(base=base)
    preflight.git(repo, "push", "guard-test", "HEAD:refs/heads/main")
    assert preflight.git(repo, "ls-remote", "guard-test", "refs/heads/main").split()[0] == head
