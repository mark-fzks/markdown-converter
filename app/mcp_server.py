"""MCP server exposing the converter as tools Claude can call.

Mounted at ``/mcp`` on the same app over the Streamable HTTP transport. Text
formats come back inline in the tool result; binaries (PDF, Word, …) are parked
in the download store and returned as a short-lived link, because an MCP tool
result carries text, not files.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Annotated, Any

import anyio
import httpx
from mcp.server import MCPServer
from pydantic import Field

from .config import Settings, get_settings
from .convert import ConversionError, ConvertOptions, convert_file, sanitize_filename
from .downloads import DownloadStore
from .formats import FORMATS, HIGHLIGHT_STYLES, PAPER_SIZES, PDF_ENGINES, get_format
from .themes import list_themes

log = logging.getLogger(__name__)

#: Formats whose output is human-readable and small enough to inline.
INLINE_FORMATS: frozenset[str] = frozenset(
    {
        "html-fragment", "latex", "typst", "gfm", "commonmark", "rst", "asciidoc",
        "org", "mediawiki", "dokuwiki", "textile", "man", "texinfo", "plain",
        "json", "opml", "docbook", "jats",
    }
)

#: Above this, even a text result is summarised rather than pasted into context.
MAX_INLINE_CHARS = 60_000

INSTRUCTIONS = """\
Converts Markdown into PDF, Word, and ~25 other formats via pandoc.

Call `list_formats` first if you are unsure of a format key — passing an unknown
one is an error. Text formats (html-fragment, latex, rst, gfm, …) are returned
inline. Binary formats (pdf, docx, odt, pptx, epub, html) are returned as a
download link that expires, because tool results cannot carry files.
"""


def build_mcp_server(store: DownloadStore, settings: Settings) -> MCPServer:
    mcp = MCPServer(
        name="markdown-converter",
        title="Markdown Converter",
        version="1.0.0",
        instructions=INSTRUCTIONS,
    )

    # -- helpers -----------------------------------------------------------

    def _public_url(token: str, filename: str) -> str:
        base = settings.public_base_url.rstrip("/")
        return f"{base}/download/{token}/{filename}"

    def _options(
        to: str,
        theme: str | None,
        toc: bool,
        number_sections: bool,
        papersize: str,
        pdf_engine: str | None,
        title: str | None,
        author: str | None,
    ) -> ConvertOptions:
        try:
            get_format(to)
        except ValueError as exc:
            raise ValueError(f"{exc}. Call list_formats to see valid keys.") from None
        if papersize not in PAPER_SIZES:
            raise ValueError(f"unknown paper size {papersize!r}; expected one of {', '.join(PAPER_SIZES)}")

        metadata: dict[str, str] = {}
        if title:
            metadata["title"] = title
        if author:
            metadata["author"] = author

        return ConvertOptions(
            to=to,
            theme=theme or None,
            toc=toc,
            number_sections=number_sections,
            papersize=papersize,
            pdf_engine=pdf_engine or None,
            metadata=metadata,
        )

    def _run(markdown: str, name: str, options: ConvertOptions) -> dict[str, Any]:
        """Convert in a scratch directory and shape the result for a tool reply."""
        workdir = Path(tempfile.mkdtemp(prefix="mdconv-mcp-"))
        try:
            stem = Path(sanitize_filename(name)).stem or "document"
            source = workdir / f"{stem}.md"
            source.write_text(markdown, encoding="utf-8")

            outcome = convert_file(source, options, settings, workdir / "out")
            fmt = options.output_format
            result: dict[str, Any] = {
                "format": fmt.key,
                "filename": outcome.path.name,
                "bytes": outcome.path.stat().st_size,
            }
            if outcome.warnings:
                result["warnings"] = outcome.warnings

            if fmt.key in INLINE_FORMATS:
                text = outcome.path.read_text(encoding="utf-8", errors="replace")
                if len(text) <= MAX_INLINE_CHARS:
                    result["content"] = text
                    return result
                result["note"] = (
                    f"Output is {len(text)} characters — too large to inline, "
                    "returned as a download instead."
                )

            entry = store.add(outcome.path, outcome.path.name, fmt.mime)
            result["download_url"] = _public_url(entry.token, entry.filename)
            result["expires_in_seconds"] = store.ttl_seconds
            return result
        finally:
            import shutil

            shutil.rmtree(workdir, ignore_errors=True)

    async def _convert_async(markdown: str, name: str, options: ConvertOptions) -> dict[str, Any]:
        try:
            return await anyio.to_thread.run_sync(_run, markdown, name, options)
        except ConversionError as exc:
            raise ValueError(f"conversion failed: {exc}") from None

    # -- tools -------------------------------------------------------------

    @mcp.tool(
        title="List output formats",
        description=(
            "List every format this server can convert Markdown into, plus the "
            "available PDF engines, paper sizes and themes. Call this before "
            "`convert` when you are not certain a format key is valid."
        ),
    )
    async def list_formats() -> dict[str, Any]:
        return {
            "formats": [
                {"key": f.key, "label": f.label, "group": f.group, "returns":
                 "inline text" if f.key in INLINE_FORMATS else "download link"}
                for f in FORMATS
            ],
            "pdf_engines": list(PDF_ENGINES),
            "paper_sizes": list(PAPER_SIZES),
            "highlight_styles": list(HIGHLIGHT_STYLES),
            "themes": [t.name for t in list_themes(settings)],
            "default_theme": settings.default_theme,
        }

    @mcp.tool(
        title="Convert Markdown",
        description=(
            "Convert Markdown text into another format. Text formats are returned "
            "inline; PDF, Word and other binaries are returned as a download link "
            "that expires after a few minutes."
        ),
    )
    async def convert(
        markdown: Annotated[str, Field(description="The Markdown source to convert.")],
        to: Annotated[str, Field(description="Target format key, e.g. 'pdf', 'docx', 'html'.")] = "pdf",
        filename: Annotated[str, Field(description="Base name for the result, without extension.")] = "document",
        theme: Annotated[str | None, Field(description="Theme name; see list_formats.")] = None,
        toc: Annotated[bool, Field(description="Generate a table of contents.")] = False,
        number_sections: Annotated[bool, Field(description="Number the headings.")] = False,
        papersize: Annotated[str, Field(description="a4, letter, a5 or legal.")] = "a4",
        pdf_engine: Annotated[str | None, Field(description="'typst' or 'weasyprint'; PDF only.")] = None,
        title: Annotated[str | None, Field(description="Document title, overriding front-matter.")] = None,
        author: Annotated[str | None, Field(description="Document author.")] = None,
    ) -> dict[str, Any]:
        options = _options(to, theme, toc, number_sections, papersize, pdf_engine, title, author)
        return await _convert_async(markdown, filename, options)

    @mcp.tool(
        title="Convert a Markdown file",
        description=(
            "Fetch a Markdown document from an http(s) URL, or read it from a path "
            "inside the converter container, and convert it. Use this instead of "
            "`convert` for large documents, to keep them out of the conversation."
        ),
    )
    async def convert_file_from(
        source: Annotated[str, Field(description="An http(s) URL, or an absolute path inside the container.")],
        to: Annotated[str, Field(description="Target format key, e.g. 'pdf', 'docx'.")] = "pdf",
        theme: Annotated[str | None, Field(description="Theme name.")] = None,
        toc: Annotated[bool, Field(description="Generate a table of contents.")] = False,
        number_sections: Annotated[bool, Field(description="Number the headings.")] = False,
        papersize: Annotated[str, Field(description="a4, letter, a5 or legal.")] = "a4",
        pdf_engine: Annotated[str | None, Field(description="'typst' or 'weasyprint'; PDF only.")] = None,
    ) -> dict[str, Any]:
        markdown, name = await _read_source(source, settings)
        options = _options(to, theme, toc, number_sections, papersize, pdf_engine, None, None)
        return await _convert_async(markdown, name, options)

    @mcp.tool(
        title="Preview as HTML",
        description=(
            "Render Markdown to an HTML fragment and return it inline, so you can "
            "check structure and formatting before committing to a PDF or Word file."
        ),
    )
    async def preview(
        markdown: Annotated[str, Field(description="The Markdown source to render.")],
        theme: Annotated[str | None, Field(description="Theme name.")] = None,
    ) -> dict[str, Any]:
        options = _options("html-fragment", theme, False, False, "a4", None, None, None)
        return await _convert_async(markdown, "preview", options)

    return mcp


async def _read_source(source: str, settings: Settings) -> tuple[str, str]:
    """Resolve a URL or container path to (markdown, base name)."""
    if source.startswith(("http://", "https://")):
        limit = settings.max_upload_bytes
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
                response = await client.get(source)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ValueError(f"could not fetch {source}: {exc}") from None
        if len(response.content) > limit:
            raise ValueError(f"document exceeds the {limit // (1024 * 1024)} MB limit")
        name = Path(httpx.URL(source).path).name or "document.md"
        return response.text, name

    path = Path(source)
    if not path.is_absolute():
        raise ValueError("a local source must be an absolute path inside the container")
    if not path.is_file():
        raise ValueError(f"no such file in the converter container: {source}")
    if path.stat().st_size > settings.max_upload_bytes:
        raise ValueError("document exceeds the configured size limit")
    return path.read_text(encoding="utf-8", errors="replace"), path.name
