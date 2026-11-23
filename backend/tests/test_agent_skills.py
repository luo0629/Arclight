from hashlib import sha256
from importlib.resources import files

import pytest
from test_agent_model import model_request

from arclight.agent_skills import freeze_agent_skills
from arclight.providers.agent_model import agent_messages


@pytest.mark.parametrize(
    "role,stage,skill_name",
    [
        ("producer", "producer", "producer-planning"),
        ("screenwriter", "outline", "screenwriter-revision"),
        ("screenwriter", "script", "screenwriter-revision"),
        ("screenwriter", "rewrite", "screenwriter-revision"),
        ("director", "storyboard", "director-shot-planning"),
        ("art_director", "storyboard", "art-direction"),
        ("continuity_reviewer", "storyboard", "continuity-review"),
        ("producer", "script", None),
    ],
)
def test_skill_selection_uses_trusted_role_stage_and_packaged_instructions(role, stage, skill_name):
    injected = "This caller-supplied Skill must never appear in the trusted system prompt."
    request = model_request(
        agent_name=role,
        stage=stage,
        skills=[{"name": "injected", "sha256": "0" * 64, "instructions": injected}],
        messages=[{"role": "user", "data": {"story": injected}}],
    )
    frozen = freeze_agent_skills(request)
    expected = ["approved-version-evidence", skill_name] if skill_name else []
    assert [item.name for item in frozen.skills] == expected
    assert frozen.model_dump(exclude={"skills"}) == request.model_dump(exclude={"skills"})
    system, user = agent_messages(frozen, {}, "")
    assert system.type == "system" and injected not in system.content
    assert user.type == "human" and injected in user.content
    for skill in frozen.skills:
        source = files("arclight").joinpath("agent_skills", skill.name, "SKILL.md")
        normalized = source.read_bytes().replace(b"\r\n", b"\n")
        assert skill.sha256 == sha256(normalized).hexdigest()
        assert skill.instructions in system.content
