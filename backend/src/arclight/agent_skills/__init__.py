"""Resolve trusted, packaged Agent Skills when a new execution starts."""

from hashlib import sha256
from importlib.resources import files

import yaml
from pydantic import ValidationError

from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelRequest, AgentSkillSnapshot
from arclight.domain.agent_tools import AgentName

_SHARED_SKILL = "approved-version-evidence"
_ROLE_SKILLS: dict[AgentName, tuple[str, frozenset[str]]] = {
    "producer": ("producer-planning", frozenset({"producer"})),
    "screenwriter": ("screenwriter-revision", frozenset({"outline", "script", "rewrite"})),
    "director": ("director-shot-planning", frozenset({"storyboard"})),
    "art_director": ("art-direction", frozenset({"storyboard"})),
    "continuity_reviewer": ("continuity-review", frozenset({"storyboard"})),
}


def _load_skill(name: str) -> AgentSkillSnapshot:
    # Normalize checkout line endings before hashing the complete Skill source.
    source = files("arclight.agent_skills").joinpath(name, "SKILL.md").read_text(encoding="utf-8")
    source = source.replace("\r\n", "\n")
    metadata, separator, instructions = source.removeprefix("---\n").partition("\n---\n")
    if not source.startswith("---\n") or not separator or len(source.encode()) > 16384:
        raise ValueError(f"Invalid Agent Skill frontmatter: {name}")
    header = yaml.safe_load(metadata)
    description = header.get("description") if isinstance(header, dict) else None
    if (
        not isinstance(header, dict)
        or header.get("name") != name
        or not isinstance(description, str)
        or not 0 < len(description.strip()) <= 1024
    ):
        raise ValueError(f"Invalid Agent Skill metadata: {name}")
    return AgentSkillSnapshot(
        name=name, sha256=sha256(source.encode()).hexdigest(), instructions=instructions.strip()
    )


def freeze_agent_skills(request: AgentModelRequest) -> AgentModelRequest:
    """Select by server-owned role and stage, then bind exact instructions to the request."""
    name, stages = _ROLE_SKILLS[request.agent_name]
    skills = (_load_skill(_SHARED_SKILL), _load_skill(name)) if request.stage in stages else ()
    try:
        return AgentModelRequest.model_validate(
            request.model_dump(exclude={"skills"})
            | {
                "skills": skills,
            }
        )
    except ValidationError as exc:
        raise DomainError("AGENT_CONTEXT_LIMIT", "Agent 输入与技能总量超限", 422) from exc
