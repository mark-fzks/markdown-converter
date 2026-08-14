from __future__ import annotations

import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from app.archive import ArchiveError, safe_extract
from app.convert import sanitize_filename
from app.formats import FORMATS, FORMATS_BY_KEY, get_format
from app.mermaid import _split_blocks, render_mermaid_blocks
from app.themes import is_safe_theme_name, list_themes, load_theme


# --- formats ---------------------------------------------------------------


def test_format_keys_are_unique():
    assert len(FORMATS_BY_KEY) == len(FORMATS)


@pytest.mark.parametrize("key", ["pdf", "docx", "odt", "html", "epub", "pptx", "latex"])
def test_expected_formats_are_present(key):
    assert get_format(key).key == key


def test_unknown_format_raises():
    with pytest.raises(ValueError):
        get_format("wingdings")


# --- filenames -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("notes.md", "notes.md"),
        ("../../etc/passwd", "passwd"),
        ("/absolute/path/report.md", "report.md"),
        ("weird;name|here.md", "weird_name_here.md"),
        ("...", "document"),
        ("", "document"),
    ],
)
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected


# --- themes ----------------------------------------------------------------


@pytest.mark.parametrize("name", ["../../etc/passwd", "..", "a/b", ".hidden", "", "x" * 100])
def test_unsafe_theme_names_rejected(name):
    assert not is_safe_theme_name(name)


def test_default_theme_has_css(settings):
    theme = load_theme("default", settings)
    assert theme.css is not None and theme.css.is_file()


def test_list_themes_includes_default(settings):
    assert "default" in {t.name for t in list_themes(settings)}


def test_load_theme_rejects_traversal(settings):
    with pytest.raises(ValueError):
        load_theme("../../etc/passwd", settings)


# --- mermaid block scanning ------------------------------------------------


def test_split_blocks_finds_mermaid():
    text = "intro\n\n```mermaid\ngraph TD;\nA-->B;\n```\n\noutro\n"
    chunks = _split_blocks(text)
    assert [is_mermaid for is_mermaid, _ in chunks] == [False, True, False]
    assert "graph TD" in chunks[1][1]


def test_split_blocks_leaves_other_code_alone():
    text = "```python\nprint('mermaid')\n```\n"
    assert [is_mermaid for is_mermaid, _ in _split_blocks(text)] == [False]


def test_failed_mermaid_render_degrades_to_code_block(tmp_path, settings):
    live = replace(settings, mermaid_enabled=True, mermaid_bin="definitely-not-installed")
    result = render_mermaid_blocks("```mermaid\ngraph TD;\nA-->B;\n```\n", tmp_path, live)

    assert result.rendered == 0
    assert result.failures
    # The diagram source survives as a plain code block instead of vanishing.
    assert "graph TD" in result.markdown
    assert "```mermaid" not in result.markdown


# --- archives --------------------------------------------------------------


def _zip_with(tmp_path: Path, members: dict[str, str]) -> Path:
    archive = tmp_path / "upload.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return archive


def test_safe_extract_reads_normal_archive(tmp_path):
    archive = _zip_with(tmp_path, {"a.md": "# A", "docs/b.md": "# B"})
    extracted = safe_extract(archive, tmp_path / "out", max_bytes=1 << 20, max_files=10)
    assert {p.name for p in extracted} == {"a.md", "b.md"}


def test_safe_extract_blocks_path_traversal(tmp_path):
    archive = _zip_with(tmp_path, {"../escaped.md": "# nope", "ok.md": "# yes"})
    dest = tmp_path / "out"
    extracted = safe_extract(archive, dest, max_bytes=1 << 20, max_files=10)

    assert [p.name for p in extracted] == ["ok.md"]
    assert not (tmp_path / "escaped.md").exists()


def test_safe_extract_blocks_absolute_paths(tmp_path):
    archive = _zip_with(tmp_path, {"/etc/evil.md": "# nope", "ok.md": "# yes"})
    extracted = safe_extract(archive, tmp_path / "out", max_bytes=1 << 20, max_files=10)
    assert [p.name for p in extracted] == ["ok.md"]


def test_safe_extract_rejects_too_many_files(tmp_path):
    archive = _zip_with(tmp_path, {f"{i}.md": "x" for i in range(12)})
    with pytest.raises(ArchiveError):
        safe_extract(archive, tmp_path / "out", max_bytes=1 << 20, max_files=10)


def test_safe_extract_rejects_zip_bomb(tmp_path):
    archive = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big.md", "a" * (5 << 20))
    with pytest.raises(ArchiveError):
        safe_extract(archive, tmp_path / "out", max_bytes=1 << 20, max_files=10)


def test_safe_extract_rejects_garbage(tmp_path):
    archive = tmp_path / "not.zip"
    archive.write_bytes(b"definitely not a zip")
    with pytest.raises(ArchiveError):
        safe_extract(archive, tmp_path / "out", max_bytes=1 << 20, max_files=10)
