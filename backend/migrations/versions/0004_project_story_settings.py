"""Add a bounded project story brief; old projects retain the 30-second default."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "story_settings",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    # Refuse to silently discard business settings. Defaults/legacy empty briefs are safe.
    if not context.is_offline_mode():
        projects = sa.table("projects", sa.column("story_settings", sa.JSON()))
        defaults = {
            "schema_version": 1,
            "description": "",
            "genre": "",
            "target_audience": "",
            "content_restrictions": "",
            "default_episode_duration_ms": 30000,
            "target_episode_count": 1,
            "episode_plan": "",
        }
        for brief in op.get_bind().scalars(sa.select(projects.c.story_settings)):
            if brief and brief != defaults:
                raise RuntimeError(
                    "Project story settings exist; back up and migrate before downgrade"
                )
    op.drop_column("projects", "story_settings")
