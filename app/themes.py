"""Discovery of user-supplied themes.

A theme is just a name that may have any of these files under ``THEMES_DIR``::

    themes/css/<name>.css            styling for HTML, EPUB, reveal.js, WeasyPrint PDFs
    themes/typst/<name>.typ          pandoc template used by the Typst PDF engine
    themes/reference/<name>.docx     --reference-doc for Word output
    themes/reference/<name>.odt      --reference-doc for ODT output
    themes/reference/<name>.pptx     --reference-doc for PowerPoint output

Nothing is mandatory: a theme with only a ``.css`` file simply has no effect on
Word output. Mount the directory as a volume to add your own house style
without rebuilding the image.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_REFERENCE_SUFFIXES = (".docx", ".odt", ".pptx")


@dataclass(frozen=True)
class Theme:
    name: str
    css: Path | None = None
    typst_template: Path | None = None
    references: dict[str, Path] = field(default_factory=dict)

    def reference_for(self, ext: str) -> Path | None:
        return self.references.get(ext)

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "css": self.css is not None,
            "typst_template": self.typst_template is not None,
            "reference_docs": sorted(self.references),
        }


def is_safe_theme_name(name: str) -> bool:
    """Reject anything that could escape ``THEMES_DIR`` or hit a dotfile."""
    return bool(_SAFE_NAME.match(name)) and ".." not in name


def load_theme(name: str, settings: Settings) -> Theme:
    if not is_safe_theme_name(name):
        raise ValueError(f"invalid theme name: {name!r}")

    root = settings.themes_dir
    css = root / "css" / f"{name}.css"
    typst = root / "typst" / f"{name}.typ"

    references: dict[str, Path] = {}
    for suffix in _REFERENCE_SUFFIXES:
        candidate = root / "reference" / f"{name}{suffix}"
        if candidate.is_file():
            references[suffix] = candidate

    return Theme(
        name=name,
        css=css if css.is_file() else None,
        typst_template=typst if typst.is_file() else None,
        references=references,
    )


def list_themes(settings: Settings) -> list[Theme]:
    """Every theme name that has at least one file backing it."""
    root = settings.themes_dir
    names: set[str] = set()

    for subdir in ("css", "typst", "reference"):
        directory = root / subdir
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if not path.is_file():
                continue
            if subdir == "css" and path.suffix != ".css":
                continue
            if subdir == "typst" and path.suffix != ".typ":
                continue
            if subdir == "reference" and path.suffix not in _REFERENCE_SUFFIXES:
                continue
            if is_safe_theme_name(path.stem):
                names.add(path.stem)

    return [load_theme(name, settings) for name in sorted(names)]
