"""Trusted adapter pricing boundary. Client parameters never supply a price."""

from decimal import Decimal

from arclight.core.errors import DomainError
from arclight.core.image_service import OpenAIImageSettings
from arclight.domain.generation_budget import CostQuote
from arclight.domain.generation_requests import GenerationSpec


def quote(
    spec: GenerationSpec, task_type: str, image_service: OpenAIImageSettings | None = None
) -> CostQuote | None:
    # Real adapters must supply a bounded quote before they can become billable.
    if spec.provider == "mock":
        return CostQuote(maximum_cost=Decimal("0"), pricing_version="mock-free-v1")
    if spec.provider == "openai" and image_service is not None:
        image_service.require_ready()
        configuration = image_service.public_configuration()
        if (
            task_type != "IMAGE"
            or spec.model != image_service.model
            or spec.model_config_version != configuration["configuration_version"]
        ):
            raise DomainError(
                "IMAGE_SERVICE_CONFIG_CHANGED", "图像服务配置版本不匹配，请重新读取配置", 409
            )
        from arclight.providers.openai_images import validate_spec

        validate_spec(spec)
        assert image_service.maximum_cost_per_image is not None
        return CostQuote(
            maximum_cost=image_service.maximum_cost_per_image * spec.batch_size,
            pricing_version=image_service.pricing_version,
        )
    return None
