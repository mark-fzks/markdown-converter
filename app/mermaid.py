"""Turn ```mermaid fenced blocks into real images before pandoc sees them."""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)

_FENCE = re.compile(r"^(?P<indent>[ ]{0,3})(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
# ```mermaid, ```{.mermaid}, ```mermaid {caption="..."} all count
_MERMAID_INFO = re.compile(r"^\{?\.?\s*mermaid\b", re.IGNORECASE)


@dataclass
class MermaidResult:
    markdown: str
    rendered: int = 0
    failures: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.failures is None:
            self.failures = []


def _is_mermaid_info(info: str) -> bool:
    return bool(_MERMAID_INFO.match(info.strip()))


def _split_blocks(markdown: str) -> list[tuple[bool, str]]:
    """Split into ``(is_mermaid, text)`` chunks, respecting nested fences."""
    lines = markdown.splitlines(keepends=True)
    chunks: list[tuple[bool, str]] = []
    buffer: list[str] = []
    index = 0

    while index < len(lines):
        match = _FENCE.match(lines[index].rstrip("\n"))
        if not match or not _is_mermaid_info(match.group("info")):
            buffer.append(lines[index])
            index += 1
            continue

        # Found the opening fence of a mermaid block: find its closing fence.
        fence = match.group("fence")
        closing = re.compile(rf"^[ ]{{0,3}}{fence[0]}{{{len(fence)},}}\s*$")
        body: list[str] = []
        index += 1
        while index < len(lines) and not closing.match(lines[index].rstrip("\n")):
            body.append(lines[index])
            index += 1
        index += 1  # step over the closing fence (or off the end)

        if buffer:
            chunks.append((False, "".join(buffer)))
            buffer = []
        chunks.append((True, "".join(body)))

    if buffer:
        chunks.append((False, "".join(buffer)))
    return chunks


def render_mermaid_blocks(
    markdown: str,
    workdir: Path,
    settings: Settings,
    image_format: str = "png",
) -> MermaidResult:
    """Replace every mermaid block with an image reference.

    Images land in ``workdir`` so that pandoc resolves them relative to the
    document. A block that fails to render is left as a plain code block rather
    than failing the whole conversion.
    """
    if not settings.mermaid_enabled or "mermaid" not in markdown.lower():
        return MermaidResult(markdown)

    chunks = _split_blocks(markdown)
    if not any(is_mermaid for is_mermaid, _ in chunks):
        return MermaidResult(markdown)

    assets = workdir / "_mermaid"
    assets.mkdir(parents=True, exist_ok=True)

    out: list[str] = []
    result = MermaidResult("", 0, [])
    counter = 0

    for is_mermaid, text in chunks:
        if not is_mermaid:
            out.append(text)
            continue

        counter += 1
        source = assets / f"diagram-{counter}.mmd"
        target = assets / f"diagram-{counter}.{image_format}"
        source.write_text(text, encoding="utf-8")

        try:
            _run_mmdc(source, target, settings)
        except Exception as exc:  # noqa: BLE001 - degrade, never fail the job
            log.warning("mermaid block %d failed to render: %s", counter, exc)
            result.failures.append(f"diagram {counter}: {exc}")
            out.append(f"```\n{text.rstrip()}\n```\n")
            continue

        rel = target.relative_to(workdir).as_posix()
        out.append(f"\n![]({rel})\n")
        result.rendered += 1

    result.markdown = "".join(out)
    return result


def _run_mmdc(source: Path, target: Path, settings: Settings) -> None:
    cmd = [
        settings.mermaid_bin,
        "--input", str(source),
        "--output", str(target),
        "--backgroundColor", "transparent",
        "--theme", settings.mermaid_theme,
    ]
    if target.suffix == ".png":
        cmd += ["--scale", str(settings.mermaid_scale)]
    if Path(settings.puppeteer_config).is_file():
        cmd += ["--puppeteerConfigFile", settings.puppeteer_config]

    proc = subprocess.run(
        cmd,
        cwd=source.parent,
        capture_output=True,
        text=True,
        timeout=settings.conversion_timeout,
        check=False,
    )
    if proc.returncode != 0 or not target.is_file():
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else f"mmdc exited {proc.returncode}")
