"""Use a synthetic frozen account route in adapter and Agent integration tests."""

from cryptography.fernet import Fernet
from pydantic import SecretStr

from arclight.application import model_services
from arclight.application.service_credentials import store_credential
from arclight.domain.service_models import ServiceModelPolicy, matching_policy


def use_test_text_service(settings, monkeypatch):
    """Keep fixture project ownership while supplying a real credential snapshot."""
    if not settings.service_encryption_key.get_secret_value():
        settings.service_encryption_key = SecretStr(Fernet.generate_key().decode())
    select_account = model_services.text_snapshot

    def select_fixture(db, actor):
        selected = select_account(db, actor)
        if selected is not None or settings.provider != "openai_compatible":
            return selected
        profile = {
            "supplier": settings.llm_supplier,
            "base_url": settings.llm_base_url,
            "model": settings.llm_model,
            "protocol": settings.llm_protocol,
        }
        configuration = {"kind": "text", **profile}
        existing = matching_policy(settings.service_model_policies, configuration)
        settings.service_model_policies = [
            item for item in settings.service_model_policies if item is not existing
        ] + [
            ServiceModelPolicy(**configuration, agent_capabilities=settings.llm_agent_capabilities)
        ]
        key = settings.llm_api_key.get_secret_value() or "synthetic-text-key"
        credential = store_credential(db, settings, key)
        return {
            **profile,
            "supplier_name": "",
            "response_format": settings.llm_response_format,
            "credential_id": str(credential.id),
        }

    monkeypatch.setattr(model_services, "text_snapshot", select_fixture)
