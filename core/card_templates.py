"""Built-in starting templates for the /personalizar composer.

Templates are code (not per-guild) so they stay reviewed and translated via
``cards.templates.<slug>.label/.description``. Applying a template replaces
the working draft (undoable) — it never writes to storage directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CardTemplate:
    """A named starting document for the composer."""

    slug: str
    blocks: list[dict[str, Any]]

    @property
    def label_key(self) -> str:
        return f"cards.templates.{self.slug}.label"

    @property
    def description_key(self) -> str:
        return f"cards.templates.{self.slug}.description"


def _text(body: str) -> dict[str, Any]:
    return {"type": "text", "body": body}


BUILTIN_TEMPLATES: list[CardTemplate] = [
    CardTemplate(
        slug="banner",
        blocks=[
            {
                "type": "container",
                "color": "brand",
                "children": [_text("# {title}"), {"type": "divider"}, _text("{body}")],
            }
        ],
    ),
    CardTemplate(
        slug="profile",
        blocks=[
            {
                "type": "section",
                "accessory": {"type": "thumbnail", "url": "{user_avatar}"},
                "children": [_text("# {user}"), _text("{server}")],
            }
        ],
    ),
    CardTemplate(
        slug="panel",
        blocks=[
            {
                "type": "container",
                "color": "info",
                "children": [
                    _text("# {title}"),
                    _text("{body}"),
                    {
                        "type": "row",
                        "buttons": [{"label": "{button}", "url": "https://example.com"}],
                    },
                ],
            }
        ],
    ),
]


def list_templates() -> list[CardTemplate]:
    """All built-in templates in display order."""
    return list(BUILTIN_TEMPLATES)


def get_template(slug: str) -> CardTemplate | None:
    """Find a template by slug."""
    return next((t for t in BUILTIN_TEMPLATES if t.slug == slug), None)
