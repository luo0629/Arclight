"""Script writing operations that end with an editable candidate."""

from typing import Literal

ScriptWritingPurpose = Literal["EDITABLE_DRAFT", "REVIEW_WORKFLOW"]
SCRIPT_DRAFT = "script_draft"
SCRIPT_REWRITE_DRAFT = "script_rewrite_draft"
DRAFT_WORKFLOWS = frozenset({SCRIPT_DRAFT, SCRIPT_REWRITE_DRAFT})
REWRITE_WORKFLOWS = frozenset({"script_rewrite", SCRIPT_REWRITE_DRAFT})
