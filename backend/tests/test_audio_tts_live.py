"""Explicitly authorized three-line TTS acceptance; never a normal CI provider call.

Set ARCLIGHT_RUN_LIVE_TTS=1 and every ARCLIGHT_LIVE_TTS_* input listed in
LiveInputs.load. AUTHORIZATION must explicitly authorize these three original
lines, their provider transfer, output rights and the three-line USD cap.
MAX_USD_PER_LINE is an operator-verified conservative quote, not measured cost.
Use a new EVIDENCE_DIR for each authorized run. An existing evidence.json blocks
execution, including after an uncertain result. Never retry this test to resolve
uncertain provider acceptance. WAVs require a separate human listening decision.
"""

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select
from test_generation_budget import set_policy
from test_generation_delivery import event_for
from test_model_services import account
from test_text_contract_v2 import save_script

from arclight.application.generation_artifacts import import_attempt
from arclight.application.generation_delivery import consume_dispatch
from arclight.application.generation_scheduling import policy_for
from arclight.application.media_services import connection_for_job
from arclight.core.config import Settings
from arclight.domain.generation_scheduling import ExecutionPolicy
from arclight.domain.model_gateway import GatewayPolicy
from arclight.domain.service_models import ServiceModelPolicy
from arclight.persistence.generation_budget import GenerationBudgetQuote
from arclight.persistence.models import GenerationAttempt, GenerationJob, StoredFile
from arclight.providers.native_media import NativeMediaProvider

pytestmark = pytest.mark.skipif(
    os.getenv("ARCLIGHT_RUN_LIVE_TTS") != "1"
    or not os.getenv("ARCLIGHT_LIVE_TTS_AUTHORIZATION", "").strip(),
    reason="Paid TTS requires explicit authorization, dedicated credentials and a USD cap",
)

ORIGINAL_LINES = (
    ("小岚", "雨停了，我们现在出发吧。"),
    ("小岚", "别怕，我会一直陪着你。"),
    ("旁白", "清晨，街角传来第一声鸟鸣。"),
)


@dataclass(frozen=True)
class LiveInputs:
    key_file: Path
    base_url: str
    model: str
    protocol: str
    voice: str
    quote: Decimal
    pricing_version: str
    evidence_dir: Path

    @classmethod
    def load(cls):
        def required(name):
            value = os.getenv(f"ARCLIGHT_LIVE_TTS_{name}", "").strip()
            if not value:
                pytest.fail(f"Missing explicit ARCLIGHT_LIVE_TTS_{name}", pytrace=False)
            return value

        # No defaults for provider, credentials, pricing or evidence destination.
        values = {
            name: required(name)
            for name in (
                "KEY_FILE",
                "BASE_URL",
                "MODEL",
                "PROTOCOL",
                "VOICE",
                "MAX_USD_PER_LINE",
                "PRICING_VERSION",
                "EVIDENCE_DIR",
            )
        }
        quote = Decimal(values["MAX_USD_PER_LINE"])
        assert quote.is_finite() and 0 < quote <= 10
        assert quote.as_tuple().exponent >= -6
        assert values["PROTOCOL"] in {"openai_tts", "siliconflow_tts", "qwen_tts"}
        return cls(
            key_file=Path(values["KEY_FILE"]).resolve(),
            base_url=values["BASE_URL"],
            model=values["MODEL"],
            protocol=values["PROTOCOL"],
            voice=values["VOICE"],
            quote=quote,
            pricing_version=values["PRICING_VERSION"],
            evidence_dir=Path(values["EVIDENCE_DIR"]).resolve(),
        )


class IsolatedSettings(Settings):
    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        # Do not read .env, business URLs, inherited encryption keys or secret files.
        return (init_settings,)


@pytest.fixture
def settings(tmp_path):
    storage = tmp_path / "objects"
    storage.mkdir()
    return IsolatedSettings(
        _env_file=None,
        environment="test",
        database_url=f"sqlite:///{tmp_path / 'live-tts-isolated.db'}",
        checkpoint_url=f"sqlite:///{tmp_path / 'live-tts-checkpoints.db'}",
        provider="mock",
        api_token="test-token",
        asset_storage_path=storage,
        object_storage_backend="local",
        service_encryption_key=SecretStr(Fernet.generate_key().decode()),
        media_execution_policy=ExecutionPolicy(
            max_attempts=1,
            retry_errors=frozenset(),
            automatic_retry_errors=frozenset(),
            call_timeout_seconds=180,
            max_elapsed_seconds=600,
        ),
        model_gateway_policy=GatewayPolicy(max_attempts=1, timeout_seconds=180),
    )


class CountedNativeProvider(NativeMediaProvider):
    """Count real POST boundaries without substituting an HTTP transport."""

    def __init__(self, settings, connection, report, persist):
        super().__init__(settings, connection)
        self.report = report
        self.persist = persist
        self.posted = False

    def _read(self, method, url, **kwargs):
        if method == "POST":
            assert not self.posted, "A line must never submit twice"
            assert self.report["provider_post_attempts"] < len(ORIGINAL_LINES)
            self.posted = True
            self.report["provider_post_attempts"] += 1
            # Persist the attempted charge boundary before entering network I/O.
            self.persist()
        return super()._read(method, url, **kwargs)


def write_new_json(path, payload):
    with path.open("x", encoding="utf-8") as destination:
        json.dump(payload, destination, ensure_ascii=False, indent=2)


def deliver_one(client, settings, inputs, report, persist, line, voice_id, index):
    entry = report["lines"][index]
    endpoint = f"/api/v1/audio-snapshots/{report['snapshot_id']}/lines/{line['line_id']}/tts"
    entry["stage"] = "create_job"
    payload = {"creation_key": str(uuid4()), "voice_version_id": voice_id}
    response = client.post(endpoint, json=payload)
    assert response.status_code == 202, "TTS creation failed"
    tts = response.json()
    entry.update(job_id=tts["job_id"], line_id=line["line_id"], voice_version_id=voice_id)
    replay = client.post(endpoint, json=payload)
    assert replay.status_code == 200 and replay.json() == tts
    factory = client.app.state.session_factory
    with factory() as db:
        job = db.get(GenerationJob, UUID(tts["job_id"]))
        frozen = policy_for(db, job)
        assert frozen.max_attempts == 1
        assert not frozen.automatic_retry_errors and not frozen.retry_errors
        quote = db.scalar(
            select(GenerationBudgetQuote).where(GenerationBudgetQuote.job_id == job.id)
        )
        assert quote is not None and quote.maximum_cost == inputs.quote
        assert quote.currency == "USD" and quote.pricing_version == inputs.pricing_version
        connection = connection_for_job(db, job, settings)
    provider = CountedNativeProvider(settings, connection, report, persist)
    assert provider.transport is None
    event_id = event_for(client, {"id": tts["job_id"]})

    def dispatch():
        return consume_dispatch(
            factory,
            UUID(tts["job_id"]),
            event_id,
            provider,
            execution_policy=settings.media_execution_policy,
            gateway_policy=settings.model_gateway_policy,
        )

    entry["stage"] = "provider_delivery"
    persist()
    entry["generation_status"] = dispatch()
    persist()
    assert entry["generation_status"] == "SUCCEEDED", "Stop; reconcile before any new submission"
    count = report["provider_post_attempts"]
    assert dispatch() == "SUCCEEDED" and report["provider_post_attempts"] == count
    entry["duplicate_delivery_no_new_post"] = True
    with factory() as db:
        attempts = list(
            db.scalars(
                select(GenerationAttempt).where(GenerationAttempt.job_id == UUID(tts["job_id"]))
            )
        )
        assert len(attempts) == 1 and attempts[0].status == "SUCCEEDED"
        attempt_id = attempts[0].id
    entry["stage"] = "private_import"
    assert import_attempt(factory, settings, attempt_id, provider) == 1
    assert import_attempt(factory, settings, attempt_id, provider) == 0
    assert report["provider_post_attempts"] == count
    entry["duplicate_import_no_new_post"] = True
    result_url = endpoint + f"/{tts['job_id']}/result"
    entry["stage"] = "explicit_rights_registration"
    declaration = {
        "expected_revision": 0,
        "source_note": "明确授权的三句原创台词真实TTS验收",
        "rights_note": "操作者已明确授权本次生成结果的测试保存与使用",
        "rights_confirmed": True,
    }
    response = client.post(result_url, json=declaration)
    assert response.status_code == 201, "Explicit result registration failed"
    receipt = response.json()
    replay = client.post(result_url, json=declaration)
    assert replay.status_code == 200 and replay.json() == receipt
    assert client.get(result_url).json() == receipt
    assert receipt["binding"]["line_id"] == line["line_id"]
    assert receipt["voice_version_id"] == voice_id
    assert receipt["validation"]["silence"]["silent"] is False
    artifact_url = (
        f"/api/v1/projects/{report['project_id']}/generation-jobs/{tts['job_id']}/artifacts"
    )
    response = client.get(artifact_url)
    assert response.status_code == 200
    artifacts = response.json()
    assert len(artifacts) == 1 and artifacts[0]["error_code"] is None
    entry["stage"] = "download_wav"
    download = client.get(artifact_url + f"/{artifacts[0]['id']}/content")
    assert download.status_code == 200
    assert download.headers["cache-control"] == "private, no-store"
    digest = hashlib.sha256(download.content).hexdigest()
    with factory() as db:
        exact = db.get(StoredFile, UUID(receipt["binding"]["file_id"]))
        assert exact.rights_confirmed and exact.checksum == digest
    prefix = f"line-{index + 1:02d}"
    with (inputs.evidence_dir / f"{prefix}.wav").open("xb") as destination:
        destination.write(download.content)
    write_new_json(inputs.evidence_dir / f"{prefix}-receipt.json", receipt)
    write_new_json(inputs.evidence_dir / f"{prefix}-machine-report.json", receipt["validation"])
    entry.update(
        stage="machine_pass_human_listening_pending",
        sha256=digest,
        wav=f"{prefix}.wav",
        receipt=f"{prefix}-receipt.json",
        machine_report=f"{prefix}-machine-report.json",
        asset_version_id=receipt["binding"]["asset_version_id"],
    )
    persist()


def test_three_authorized_original_tts_lines(settings, client):
    inputs = LiveInputs.load()
    assert settings.database_url.startswith("sqlite:///")
    assert settings.checkpoint_url.startswith("sqlite:///")
    inputs.evidence_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "authorization_present": True,
        "protocol": inputs.protocol,
        "pricing_version": inputs.pricing_version,
        "model": inputs.model,
        "voice": inputs.voice,
        "maximum_usd_per_line": str(inputs.quote),
        "maximum_usd_project": str(inputs.quote * 3),
        "quote_is_actual_cost": False,
        "provider_post_attempts": 0,
        "machine_result": "NOT_COMPLETED",
        "human_listening": "PENDING",
        "lines": [
            {"speaker": speaker, "text": text, "stage": "not_started"}
            for speaker, text in ORIGINAL_LINES
        ],
    }
    # Exclusive creation is the durable rerun guard, before reading credentials.
    try:
        evidence = (inputs.evidence_dir / "evidence.json").open("x", encoding="utf-8")
    except FileExistsError:
        pytest.fail("Evidence already exists; stop and reconcile the previous run", pytrace=False)
    with evidence:

        def persist():
            evidence.seek(0)
            json.dump(report, evidence, ensure_ascii=False, indent=2)
            evidence.truncate()
            evidence.flush()
            os.fsync(evidence.fileno())

        persist()
        try:
            report["stage"] = "dedicated_credential_and_pricing"
            settings.service_model_policies = [
                ServiceModelPolicy(
                    kind="speech",
                    supplier="custom",
                    base_url=inputs.base_url,
                    model=inputs.model,
                    protocol=inputs.protocol,
                    maximum_cost_per_output=inputs.quote,
                    pricing_version=inputs.pricing_version,
                )
            ]
            key = SecretStr(inputs.key_file.read_text(encoding="utf-8").strip())
            assert key.get_secret_value(), "Dedicated key file is empty"
            account(client)
            response = client.put(
                "/api/v1/me/model-services/speech",
                json={
                    "contract_version": 2,
                    "mode": "custom",
                    "supplier": "custom",
                    "model": inputs.model,
                    "protocol": inputs.protocol,
                    "api_key": key.get_secret_value(),
                    "base_url": inputs.base_url,
                    "expected_revision": 0,
                },
            )
            del key
            assert response.status_code == 200, "Speech service configuration failed"
            response = client.post("/api/v1/projects", json={"name": "原创逐句TTS验收"})
            assert response.status_code == 201
            report["project_id"] = response.json()["id"]
            set_policy(
                client, report["project_id"], str(inputs.quote * 3), task_limit=str(inputs.quote)
            )
            response = client.post(
                f"/api/v1/projects/{report['project_id']}/episodes",
                json={"episode_no": 1, "title": "清晨出发", "target_duration_ms": 30000},
            )
            assert response.status_code == 201
            episode = response.json()
            script = save_script(
                client,
                episode,
                {
                    "title": "清晨出发",
                    "summary": "三句原创台词的逐句配音验收",
                    "characters": ["小岚"],
                    "scenes": [
                        {
                            "scene_no": 1,
                            "title": "街角",
                            "action": "小岚准备出发。",
                            "dialogue": [
                                {"speaker": speaker, "text": text}
                                for speaker, text in ORIGINAL_LINES
                            ],
                        }
                    ],
                },
            )
            response = client.post(
                f"/api/v1/episodes/{episode['id']}/audio-snapshots",
                json={"creation_key": str(uuid4()), "script_version_id": script["id"]},
            )
            assert response.status_code == 201
            report.update(snapshot_id=response.json()["id"], script_version_id=script["id"])
            response = client.get(f"/api/v1/audio-snapshots/{report['snapshot_id']}/lines")
            assert response.status_code == 200
            lines = response.json()
            assert tuple((line["speaker"], line["text"]) for line in lines) == ORIGINAL_LINES
            voices = {}
            for speaker in dict.fromkeys(speaker for speaker, _ in ORIGINAL_LINES):
                response = client.post(
                    f"/api/v1/projects/{report['project_id']}/audio-voices",
                    json={
                        "creation_key": str(uuid4()),
                        "speaker": speaker,
                        "expected_version_no": 0,
                        "parameters": {"voice": inputs.voice},
                        "output_spec": {"format": "wav"},
                    },
                )
                assert response.status_code == 201
                voices[speaker] = response.json()["id"]
            report["stage"] = "three_sequential_lines"
            for index, line in enumerate(lines):
                deliver_one(
                    client, settings, inputs, report, persist, line, voices[line["speaker"]], index
                )
            assert report["provider_post_attempts"] == 3
            report["machine_result"] = "PASS_HUMAN_LISTENING_PENDING"
        except Exception as exc:
            # Never copy provider exceptions, keys, headers, request bodies or URLs.
            report["failure_type"] = type(exc).__name__
            report["machine_result"] = "FAILED_STOPPED_NO_RETRY"
            pytest.fail(
                "TTS acceptance stopped; inspect sanitized evidence and reconcile", pytrace=False
            )
        finally:
            try:
                for line in report["lines"]:
                    if "job_id" not in line:
                        continue
                    with client.app.state.session_factory() as db:
                        attempts = list(
                            db.scalars(
                                select(GenerationAttempt).where(
                                    GenerationAttempt.job_id == UUID(line["job_id"])
                                )
                            )
                        )
                        line["attempts"] = [
                            {
                                "id": str(attempt.id),
                                "status": attempt.status,
                                "actual_cost_usd": str(attempt.cost)
                                if attempt.cost is not None
                                else None,
                                "cost_status": "known" if attempt.cost is not None else "unknown",
                            }
                            for attempt in attempts
                        ]
                if "project_id" in report:
                    costs = client.get(f"/api/v1/projects/{report['project_id']}/costs")
                    assert costs.status_code == 200
                    report["cost_summary"] = costs.json()["summary"]
                    write_new_json(
                        inputs.evidence_dir / "cost-summary.json", report["cost_summary"]
                    )
            except Exception as exc:
                report["cost_evidence_failure_type"] = type(exc).__name__
                report["machine_result"] = "FAILED_COST_EVIDENCE"
            report["finished_at"] = datetime.now(UTC).isoformat()
            persist()
            if "cost_evidence_failure_type" in report:
                pytest.fail("Cost evidence incomplete; stop and reconcile", pytrace=False)
