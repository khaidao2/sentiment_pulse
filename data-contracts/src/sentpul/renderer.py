from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

# renderer owns the templates dir (resolved relative to this file — the correct
# root). config.py deliberately does not define it.
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# Build the environment once so Jinja's template cache stays warm across renders.
#  - autoescape OFF: we emit Python/YAML, not HTML (escaping would corrupt
#    quotes / & / > in the generated code).
#  - StrictUndefined: a missing/typo'd context var raises instead of silently
#    rendering "" — catches contract/template mismatches loudly.
#  - keep_trailing_newline: generated files end with a newline (POSIX-friendly).
_ENV = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=False,
    lstrip_blocks=True,
    trim_blocks=True,
    keep_trailing_newline=True,
    undefined=StrictUndefined,
)


def render_template(name: str, /, **context) -> str:
    """Render a template to a string.

    `name` is positional-only (the `/`) so a context variable also called
    "name" can't collide with the template-name argument.
    """
    return _ENV.get_template(name).render(**context)


def write_rendered_file(path: str | Path, content: str) -> None:
    """Write generated content to an explicit path, creating parent dirs.

    The caller decides the destination (from `contract.*_path`); this function
    never derives it from the template name.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
