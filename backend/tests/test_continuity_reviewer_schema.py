from copy import deepcopy

import pytest
from test_continuity_reviews import review_case as review_case

from arclight.application.continuity_reviews import validate_review_contract
from arclight.application.production_versions import component
from arclight.core.errors import DomainError
from arclight.domain.continuity_reviews import (
    ContinuityReview,
    ReviewReference,
    review_schema_registration,
)
from arclight.domain.versions import digest


def test_original_schema_definition_and_digest_remain_exactly_compatible():
    registration = review_schema_registration()
    assert digest(registration.model_dump(mode="json")) == (
        "cc44a6d9e401df1e083bb3a1246856464a26affe847f6b85e3ae79ca5674cce1"
    )
    extended = review_schema_registration(costume=True)
    assert extended.version == "continuity-review/1.1"
    assert extended.definition["schema"] == ContinuityReview.model_json_schema()
    old_schema = deepcopy(extended.definition["schema"])
    old_schema["$defs"]["ReviewReference"]["properties"]["asset_type"]["anyOf"][0]["enum"].remove(
        "COSTUME"
    )
    assert old_schema == registration.definition["schema"]


def test_new_schema_retains_all_existing_exact_review_contracts(client, review_case):
    with client.app.state.session_factory.begin() as db:
        original = ContinuityReview.model_validate(review_case)
        validate_review_contract(db, original, "service-owner")
        registration = review_schema_registration(costume=True)
        schema = component(
            db,
            original.project_id,
            "REVIEW",
            "SCHEMA",
            registration.name,
            registration.version,
            registration.definition,
        )
        changed = original.model_dump(mode="json")
        changed["versions"] = [
            schema if item["kind"] == "SCHEMA" else item for item in changed["versions"]
        ]
        validate_review_contract(db, ContinuityReview.model_validate(changed), "service-owner")


def test_legacy_definition_cannot_claim_to_cover_new_costume_references(client, review_case):
    review = ContinuityReview.model_validate(review_case)
    ref = review.references[0].model_dump()
    ref.update(role="REFERENCE", asset_id=review.target.asset_id, asset_type="COSTUME")
    # The version guard is independent of the later asset identity/type guard.
    review.references.append(ReviewReference.model_validate(ref))
    from arclight.application.continuity_reviews import validate_review_versions

    with client.app.state.session_factory() as db, pytest.raises(DomainError) as error:
        validate_review_versions(db, review)
    assert error.value.code == "REVIEW_VERSION_INVALID"
