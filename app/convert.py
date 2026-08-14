"""The conversion pipeline: markdown in, anything pandoc can write out."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .formats import OutputFormat, PDF_ENGINES, get_format
from .mermaid import render_mermaid_blocks
from .themes import Theme, load_theme

log = logging.getLogger(__name__)

DEFAULT_INPUT_FORMAT = "markdown"

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


class ConversionError(RuntimeError):
    """A conversion failed for a reason worth showing the user."""


def sanitize_filename(name: str, fallback: str = "document") -> str:
    """Reduce an uploaded name to a harmless basename."""
    base = Path(name.replace("\\", "/")).name
    base = _UNSAFE_NAME.sub("_", base).strip(" ._")
    return base[:120] or fallback


@dataclass
class ConvertOptions:
    to: str = "pdf"
    from_format: str = DEFAULT_INPUT_FORMAT
    pdf_engine: str | None = None
    theme: str | None = None
    toc: bool = False
    toc_depth: int = 3
    number_sections: bool = False
    papersize: str = "a4"
    fontsize: str = "11pt"
    margin: str = "2cm"
    mainfont: str | None = None
    highlight_style: str = "pygments"
    mermaid: bool = True
    embed_resources: bool = True
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def output_format(self) -> OutputFormat:
        return get_format(self.to)

    def resolved_pdf_engine(self, settings: Settings) -> str:
        engine = (self.pdf_engine or settings.default_pdf_engine).lower()
        if engine not in PDF_ENGINES:
            raise ConversionError(
                f"unknown pdf engine {engine!r}; expected one of {', '.join(PDF_ENGINES)}"
            )
        return engine


@dataclass
class ConversionOutcome:
    path: Path
    mermaid_rendered: int = 0
    warnings: list[str] = field(default_factory=list)


def convert_file(
    source: Path,
    options: ConvertOptions,
    settings: Settings,
    output_dir: Path | None = None,
) -> ConversionOutcome:
    """Convert one markdown file, returning the path of the produced file.

    ``source`` must already live inside a scratch directory; relative image
    references are resolved against its parent.
    """
    fmt = options.output_format
    workdir = source.parent
    output_dir = output_dir or workdir
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / (source.stem + fmt.ext)
    if target.resolve() == source.resolve():
        target = output_dir / f"{source.stem}-converted{fmt.ext}"

    theme = _resolve_theme(options, settings)
    warnings: list[str] = []

    prepared, mermaid_count = _prepare_source(source, options, settings, fmt, warnings)

    if fmt.key == "pdf" and options.resolved_pdf_engine(settings) == "weasyprint":
        _convert_pdf_weasyprint(prepared, target, options, settings, theme)
    else:
        _run_pandoc(_pandoc_command(prepared, target, options, settings, theme, fmt), settings, workdir)

    if not target.is_file() or target.stat().st_size == 0:
        raise ConversionError("the converter produced no output")

    return ConversionOutcome(path=target, mermaid_rendered=mermaid_count, warnings=warnings)


# --------------------------------------------------------------------------
# preparation
# --------------------------------------------------------------------------


def _resolve_theme(options: ConvertOptions, settings: Settings) -> Theme:
    name = options.theme or settings.default_theme
    try:
        return load_theme(name, settings)
    except ValueError as exc:
        raise ConversionError(str(exc)) from None


def _prepare_source(
    source: Path,
    options: ConvertOptions,
    settings: Settings,
    fmt: OutputFormat,
    warnings: list[str],
) -> tuple[Path, int]:
    """Run pre-pandoc transforms, returning the path pandoc should read."""
    if not options.mermaid or not settings.mermaid_enabled:
        return source, 0

    text = source.read_text(encoding="utf-8", errors="replace")
    # SVG keeps HTML output crisp; everything else is safest with a raster image.
    image_format = "svg" if fmt.key in {"html", "html-fragment", "revealjs", "epub"} else "png"
    result = render_mermaid_blocks(text, source.parent, settings, image_format)
    if not result.rendered and not result.failures:
        return source, 0

    warnings.extend(result.failures)
    prepared = source.with_name(f"{source.stem}.mermaid{source.suffix or '.md'}")
    prepared.write_text(result.markdown, encoding="utf-8")
    return prepared, result.rendered


# --------------------------------------------------------------------------
# pandoc
# --------------------------------------------------------------------------


def _pandoc_command(
    source: Path,
    target: Path,
    options: ConvertOptions,
    settings: Settings,
    theme: Theme,
    fmt: OutputFormat,
    *,
    writer_override: str | None = None,
    standalone_override: bool | None = None,
) -> list[str]:
    writer = writer_override or fmt.writer
    workdir = source.parent

    cmd = [
        settings.pandoc_bin,
        "--from", options.from_format,
        "--to", writer,
        "--resource-path", str(workdir),
        "--output", str(target),
    ]

    standalone = fmt.standalone if standalone_override is None else standalone_override
    if standalone:
        cmd.append("--standalone")
    if options.embed_resources and writer in {"html5", "revealjs"}:
        cmd.append("--embed-resources")
    if options.toc:
        cmd += ["--toc", "--toc-depth", str(max(1, min(options.toc_depth, 6)))]
    if options.number_sections:
        cmd.append("--number-sections")
    if options.highlight_style == "none":
        cmd.append("--no-highlight")
    else:
        cmd += ["--highlight-style", options.highlight_style]

    # Office formats take their look from a reference document.
    if fmt.reference_doc:
        reference = theme.reference_for(fmt.ext)
        if reference:
            cmd += ["--reference-doc", str(reference)]

    css = _stylesheet_for(fmt, theme, settings)
    if css and fmt.key == "epub":
        cmd += ["--css", str(css)]
    elif css and writer in {"html5", "revealjs"}:
        header = _write_style_header(workdir, css, options, inject_page_rules=False)
        cmd += ["--include-in-header", str(header)]

    if fmt.key == "revealjs":
        cmd += ["-V", f"revealjs-url={settings.revealjs_url}"]

    if fmt.key == "pdf":
        cmd += ["--pdf-engine", options.resolved_pdf_engine(settings)]
        if theme.typst_template:
            cmd += ["--template", str(theme.typst_template)]

    if fmt.key in {"pdf", "typst", "latex"}:
        cmd += ["-V", f"papersize={options.papersize}", "-V", f"fontsize={options.fontsize}"]
        if options.mainfont:
            cmd += ["-V", f"mainfont={options.mainfont}"]
        if options.number_sections:
            cmd += ["-V", "section-numbering=1.1.1"]
        margin_file = _write_margin_metadata(workdir, options.margin)
        if margin_file:
            cmd += ["--metadata-file", str(margin_file)]

    for key, value in options.metadata.items():
        cmd += ["--metadata", f"{key}={value}"]

    cmd.append(str(source))
    return cmd


def _run_pandoc(cmd: list[str], settings: Settings, cwd: Path) -> None:
    log.debug("running: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=settings.conversion_timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ConversionError(f"converter binary not found: {exc.filename}") from None
    except subprocess.TimeoutExpired:
        raise ConversionError(
            f"conversion timed out after {settings.conversion_timeout}s"
        ) from None

    if proc.returncode != 0:
        raise ConversionError(_tidy_stderr(proc.stderr or proc.stdout))


def _tidy_stderr(stderr: str, limit: int = 1500) -> str:
    text = (stderr or "").strip() or "the converter failed without a message"
    return text[-limit:]


# --------------------------------------------------------------------------
# PDF via WeasyPrint (pandoc -> self-contained HTML -> PDF)
# --------------------------------------------------------------------------


def _convert_pdf_weasyprint(
    source: Path,
    target: Path,
    options: ConvertOptions,
    settings: Settings,
    theme: Theme,
) -> None:
    workdir = source.parent
    html_path = workdir / f"{source.stem}.weasy.html"
    html_format = get_format("html")

    cmd = _pandoc_command(
        source,
        html_path,
        options,
        settings,
        theme,
        html_format,
        writer_override="html5",
        standalone_override=True,
    )
    _run_pandoc(cmd, settings, workdir)

    css = _stylesheet_for(html_format, theme, settings)
    page_rules = _write_style_header(workdir, css, options, inject_page_rules=True, only_rules=True)

    try:
        from weasyprint import CSS, HTML  # imported lazily: it is a heavy import
    except ImportError as exc:  # pragma: no cover - only when deps are missing
        raise ConversionError(f"WeasyPrint is not available: {exc}") from None

    extra = [CSS(filename=str(page_rules))] if page_rules else []
    HTML(filename=str(html_path), base_url=str(workdir)).write_pdf(
        str(target), stylesheets=extra
    )


# --------------------------------------------------------------------------
# stylesheets
# --------------------------------------------------------------------------


def _stylesheet_for(fmt: OutputFormat, theme: Theme, settings: Settings) -> Path | None:
    if not fmt.css:
        return None
    if theme.css:
        return theme.css
    fallback = settings.themes_dir / "css" / "default.css"
    return fallback if fallback.is_file() else None


_PAGE_RULES = """
@page {{
  size: {papersize};
  margin: {margin};
  @bottom-center {{
    content: counter(page);
    font-size: 9pt;
    color: #6b7280;
  }}
}}
html {{ font-size: {fontsize}; }}
"""


def _write_style_header(
    workdir: Path,
    css: Path | None,
    options: ConvertOptions,
    *,
    inject_page_rules: bool,
    only_rules: bool = False,
) -> Path | None:
    """Write the stylesheet pandoc/WeasyPrint should apply.

    ``only_rules`` produces a bare CSS file (for WeasyPrint); otherwise the file
    is an HTML ``<style>`` block suitable for ``--include-in-header``.
    """
    parts: list[str] = []
    if css and not only_rules:
        parts.append(css.read_text(encoding="utf-8", errors="replace"))
    if inject_page_rules:
        parts.append(
            _PAGE_RULES.format(
                papersize=options.papersize.upper(),
                margin=options.margin,
                fontsize=options.fontsize,
            )
        )
    if not parts:
        return None

    body = "\n".join(parts)
    if only_rules:
        path = workdir / "_page-rules.css"
        path.write_text(body, encoding="utf-8")
        return path

    path = workdir / "_theme-header.html"
    path.write_text(f"<style>\n{body}\n</style>\n", encoding="utf-8")
    return path


def _write_margin_metadata(workdir: Path, margin: str) -> Path | None:
    """Typst templates want ``margin`` as a map, which -V cannot express."""
    value = margin.strip()
    if not value or not re.fullmatch(r"[0-9.]+(cm|mm|in|pt|em)", value):
        return None
    path = workdir / "_margin.yaml"
    path.write_text(f"margin:\n  x: {value}\n  y: {value}\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# tool versions (surfaced at /api/version)
# --------------------------------------------------------------------------


def tool_versions(settings: Settings) -> dict[str, str]:
    versions: dict[str, str] = {}
    probes = {
        "pandoc": [settings.pandoc_bin, "--version"],
        "typst": [settings.typst_bin, "--version"],
        "mermaid": [settings.mermaid_bin, "--version"],
    }
    for name, cmd in probes.items():
        if shutil.which(cmd[0]) is None:
            versions[name] = "not installed"
            continue
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
            first = (proc.stdout or proc.stderr).strip().splitlines()
            versions[name] = first[0] if first else "unknown"
        except Exception:  # noqa: BLE001
            versions[name] = "unknown"

    try:
        import weasyprint

        versions["weasyprint"] = weasyprint.__version__
    except Exception:  # noqa: BLE001
        versions["weasyprint"] = "not installed"

    return versions
