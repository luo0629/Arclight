"""Versioned visual proposals carry suggestions and evidence, never write authority."""

import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictBool, StringConstraints, field_serializer, model_validator

from arclight.domain.asset_metadata import AssetFields, AssetMetadata, ShortText
from arclight.domain.asset_profiles import (
    CharacterProfile,
    PropProfile,
    ReferenceVersion,
    SceneProfile,
)
from arclight.domain.generation_requests import safe_metadata
from arclight.domain.versions import VersionRegistration
from arclight.domain.visual_bible import VisualPatch

PROPOSAL_VERSION: Literal["art-proposal/1"] = "art-proposal/1"
Checksum = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Text = Annotated[str, Field(min_length=1, max_length=4000)]
AssetKind = Literal[
    "CHARACTER", "SCENE", "PROP", "STYLE", "IMAGE", "SHOT_IMAGE", "LOCATION", "COSTUME"
]


class NewAssetTarget(AssetFields):
    mode: Literal["NEW"]


class ExistingAssetTarget(AssetFields):
    mode: Literal["EXISTING"] = "EXISTING"
    asset_id: UUID
    revision: int = Field(strict=True, ge=0)
    lock_version: int = Field(strict=True, ge=0)
    current_version_id: UUID | None
    content_sha256: Checksum


AssetTarget = Annotated[NewAssetTarget | ExistingAssetTarget, Field(discriminator="mode")]


class AssetConcept(AssetFields):
    design_id: UUID
    target: AssetTarget
    name: str = Field(min_length=1, max_length=160)
    metadata: AssetMetadata | None = None


class CharacterConcept(AssetConcept):
    kind: Literal["CHARACTER"]
    profile: CharacterProfile | None = None


class SceneConcept(AssetConcept):
    kind: Literal["SCENE"]
    profile: SceneProfile | None = None

    @model_validator(mode="after")
    def own_scene_reference(self):
        if isinstance(self.target, NewAssetTarget) and self.profile and self.profile.reference:
            raise ValueError("A new scene has no own image version; use proposal references")
        return self


class PropConcept(AssetConcept):
    kind: Literal["PROP"]
    profile: PropProfile | None = None


class StyleConcept(AssetConcept):
    kind: Literal["STYLE"]


class VisualBibleConcept(AssetFields):
    kind: Literal["VISUAL_BIBLE"]
    design_id: UUID
    version_id: UUID | None
    version_no: int = Field(strict=True, ge=0)
    content_sha256: Checksum
    fields: VisualPatch

    @field_serializer("fields")
    def explicit_fields(self, value: VisualPatch) -> dict:
        return value.model_dump(mode="json", exclude_unset=True)

    @model_validator(mode="after")
    def exact_or_legacy_base(self):
        if (self.version_no == 0) != (self.version_id is None):
            raise ValueError("Visual Bible base must be an exact version or explicit legacy v0")
        return self


ArtConcept = Annotated[
    CharacterConcept | SceneConcept | PropConcept | StyleConcept | VisualBibleConcept,
    Field(discriminator="kind"),
]


class ManualArtSource(AssetFields):
    kind: Literal["MANUAL"]
    source_id: UUID
    statement: Text


class VersionArtSource(AssetFields):
    kind: Literal["VERSION"]
    source_id: UUID
    resource_type: Literal["SCRIPT", "VISUAL_BIBLE", "ASSET_VERSION"]
    resource_id: UUID
    content_sha256: Checksum


ArtSource = Annotated[ManualArtSource | VersionArtSource, Field(discriminator="kind")]


class ArtReference(AssetFields):
    reference_id: UUID
    source_id: UUID
    asset: ReferenceVersion
    asset_type: AssetKind
    usage: Literal["APPROVED_REFERENCE", "COMPARISON"] = "APPROVED_REFERENCE"
    purpose: str = Field(min_length=1, max_length=500)


class ArtPromptFragment(AssetFields):
    fragment_id: UUID
    kind: Literal["POSITIVE", "NEGATIVE"]
    text: Annotated[str, StringConstraints(strip_whitespace=False)] = Field(
        min_length=1, max_length=8000
    )
    design_ids: list[UUID] = Field(min_length=1, max_length=16)
    source_ids: list[UUID] = Field(min_length=1, max_length=64)
    reference_ids: list[UUID] = Field(default_factory=list, max_length=32)


class ArtOption(AssetFields):
    option_id: UUID
    title: ShortText
    rationale: Text
    designs: list[ArtConcept] = Field(min_length=1, max_length=16)
    prompts: list[ArtPromptFragment] = Field(default_factory=list, max_length=16)
    source_ids: list[UUID] = Field(min_length=1, max_length=64)
    reference_ids: list[UUID] = Field(default_factory=list, max_length=32)


class ArtLockConflict(AssetFields):
    design_id: UUID
    field_path: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){0,3}$",
    )
    reason: Text


class ArtQuestion(AssetFields):
    question_id: UUID
    question: str = Field(min_length=1, max_length=1000)
    design_id: UUID | None = None


def _selection(values: list[UUID], allowed: set[UUID]) -> None:
    if len(set(values)) != len(values) or not set(values) <= allowed:
        raise ValueError("Selection must contain unique, declared identities")


def profile_references(design: ArtConcept) -> list[ReferenceVersion]:
    if isinstance(design, CharacterConcept) and design.profile:
        return [image.source for image in design.profile.images]
    if isinstance(design, SceneConcept) and design.profile and design.profile.reference:
        return [design.profile.reference]
    if isinstance(design, PropConcept) and design.profile and design.profile.holder:
        return [design.profile.holder]
    return []


class ArtProposal(AssetFields):
    schema_version: Literal["art-proposal/1"] = PROPOSAL_VERSION
    project_id: UUID
    episode_id: UUID | None = None
    origin: Literal["MANUAL", "AGENT"]
    visual_goal: Text
    constraints: list[ShortText] = Field(default_factory=list, max_length=20)
    sources: list[ArtSource] = Field(min_length=1, max_length=64)
    references: list[ArtReference] = Field(default_factory=list, max_length=32)
    options: list[ArtOption] = Field(default_factory=list, max_length=4)
    preferred_option_id: UUID | None = None
    lock_conflicts: list[ArtLockConflict] = Field(default_factory=list, max_length=64)
    questions: list[ArtQuestion] = Field(default_factory=list, max_length=32)
    requires_human_review: StrictBool = True

    @model_validator(mode="after")
    def bounded_and_consistent(self):
        sources = {source.source_id: source for source in self.sources}
        references = {ref.reference_id: ref for ref in self.references}
        options = {option.option_id: option for option in self.options}
        designs = {design.design_id: design for option in self.options for design in option.designs}
        for identities, count in (
            (sources, len(self.sources)),
            (references, len(self.references)),
            (options, len(self.options)),
            (designs, sum(len(option.designs) for option in self.options)),
        ):
            if len(identities) != count:
                raise ValueError("Proposal identities must be unique")
        if not self.requires_human_review or not (self.options or self.questions):
            raise ValueError("A proposal requires human review and options or explicit questions")
        if self.origin == "AGENT" and not any(
            isinstance(source, VersionArtSource) for source in self.sources
        ):
            raise ValueError("Agent proposals require exact version evidence")
        version_sources = [
            (source.resource_type, source.resource_id)
            for source in self.sources
            if isinstance(source, VersionArtSource)
        ]
        if len(set(version_sources)) != len(version_sources):
            raise ValueError("Exact source versions must be unique")
        if self.preferred_option_id is not None and self.preferred_option_id not in options:
            raise ValueError("Preferred option must belong to this proposal")
        if len(set(self.constraints)) != len(self.constraints):
            raise ValueError("Constraints must be unique")
        exact = set()
        for reference in self.references:
            source = sources.get(reference.source_id)
            if (
                not isinstance(source, VersionArtSource)
                or source.resource_type != "ASSET_VERSION"
                or source.resource_id != reference.asset.asset_version_id
            ):
                raise ValueError("References require their exact asset version source")
            pair = (reference.asset.asset_id, reference.asset.asset_version_id)
            if pair in exact:
                raise ValueError("Exact references must not be duplicated")
            exact.add(pair)
        fragments = []
        for option in self.options:
            _selection(option.source_ids, set(sources))
            _selection(option.reference_ids, set(references))
            selected = {references[key].source_id for key in option.reference_ids}
            if not selected <= set(option.source_ids):
                raise ValueError("Options must cite the sources of their references")
            if not option.prompts and not self.questions:
                raise ValueError("An option without prompts requires an unresolved question")
            option_designs = {design.design_id for design in option.designs}
            targets: list[tuple[str, ...]] = []
            for design in option.designs:
                if isinstance(design, VisualBibleConcept):
                    targets.append(("VISUAL_BIBLE",))
                elif isinstance(design.target, ExistingAssetTarget):
                    targets.append(("EXISTING", str(design.target.asset_id)))
                else:
                    targets.append(("NEW", design.kind, design.name))
            if len(set(targets)) != len(targets):
                raise ValueError("An option cannot propose the same target twice")
            declared = {
                (
                    references[key].asset.asset_id,
                    references[key].asset.asset_version_id,
                )
                for key in option.reference_ids
            }
            for design in option.designs:
                if any(
                    (ref.asset_id, ref.asset_version_id) not in declared
                    for ref in profile_references(design)
                ):
                    raise ValueError("Profile references must be declared in their option")
            for fragment in option.prompts:
                if not fragment.text.strip():
                    raise ValueError("Prompt fragments cannot be whitespace only")
                fragments.append(fragment.fragment_id)
                _selection(fragment.design_ids, option_designs)
                _selection(fragment.source_ids, set(option.source_ids))
                _selection(fragment.reference_ids, set(option.reference_ids))
                if not {references[key].source_id for key in fragment.reference_ids} <= set(
                    fragment.source_ids
                ):
                    raise ValueError("Prompt references require matching source evidence")
        if len(set(fragments)) != len(fragments):
            raise ValueError("Prompt fragment identities must be unique")
        locations = set()
        for conflict in self.lock_conflicts:
            conflict_design = designs.get(conflict.design_id)
            if conflict_design is None or isinstance(
                getattr(conflict_design, "target", None), NewAssetTarget
            ):
                raise ValueError("Lock conflicts require a declared existing target")
            root, *path = conflict.field_path.split(".")
            if root not in {"name", "metadata", "profile", "fields"}:
                raise ValueError("Conflict must identify a proposed content field")
            value = conflict_design.model_dump(mode="json", exclude_unset=True)
            for key in (root, *path):
                if not isinstance(value, dict) or key not in value:
                    raise ValueError("Conflict field is not part of the proposed content")
                value = value[key]
            if value is None:
                raise ValueError("Conflict must identify content actually proposed for change")
            location = (conflict.design_id, conflict.field_path)
            if location in locations:
                raise ValueError("Conflict locations must be unique")
            locations.add(location)
        if len({q.question_id for q in self.questions}) != len(self.questions):
            raise ValueError("Question identities must be unique")
        if any(q.design_id is not None and q.design_id not in designs for q in self.questions):
            raise ValueError("Question target must belong to this proposal")
        payload = self.model_dump(mode="json")
        safe_metadata(payload)
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 65536:
            raise ValueError("Art proposal exceeds its 64 KiB limit")
        return self


def proposal_schema_registration() -> VersionRegistration:
    """Use GEN-11 registration without inventing an Agent execution or model binding."""
    return VersionRegistration(
        family="ART",
        kind="SCHEMA",
        name="art.proposal",
        version=PROPOSAL_VERSION,
        definition={"schema": ArtProposal.model_json_schema(), "authority": "proposal-only/1"},
    )
