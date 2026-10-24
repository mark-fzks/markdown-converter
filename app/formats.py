"""Registry of the output formats the service exposes."""

from __future__ import annotations

from dataclasses import dataclass

OCTET = "application/octet-stream"


@dataclass(frozen=True)
class OutputFormat:
    key: str
    label: str
    ext: str
    mime: str
    writer: str
    binary: bool = False
    group: str = "Other"
    #: honours ``--reference-doc`` (Office formats)
    reference_doc: bool = False
    #: styled with a CSS theme
    css: bool = False
    #: needs ``--standalone`` to be a usable file on its own
    standalone: bool = True
    note: str = ""


FORMATS: tuple[OutputFormat, ...] = (
    # --- the ones you actually asked for ---------------------------------
    OutputFormat(
        key="rich-text",
        label="Rich text (copy & paste)",
        ext=".html",
        mime="text/html",
        writer="html5",
        group="Documents",
        css=True,
        note="Self-contained HTML meant for the clipboard; the web UI renders it "
        "and copies it as rich text. Diagrams are PNG so Word and Outlook keep them.",
    ),
    OutputFormat(
        key="docx",
        label="Word (.docx)",
        ext=".docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        writer="docx",
        binary=True,
        group="Documents",
        reference_doc=True,
    ),
    OutputFormat(
        key="pdf",
        label="PDF",
        ext=".pdf",
        mime="application/pdf",
        writer="pdf",
        binary=True,
        group="Documents",
        css=True,
        note="Rendered with Typst or WeasyPrint, selectable per request.",
    ),
    OutputFormat(
        key="odt",
        label="OpenDocument Text (.odt)",
        ext=".odt",
        mime="application/vnd.oasis.opendocument.text",
        writer="odt",
        binary=True,
        group="Documents",
        reference_doc=True,
    ),
    OutputFormat(
        key="rtf",
        label="Rich Text (.rtf)",
        ext=".rtf",
        mime="application/rtf",
        writer="rtf",
        group="Documents",
    ),
    OutputFormat(
        key="pptx",
        label="PowerPoint (.pptx)",
        ext=".pptx",
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        writer="pptx",
        binary=True,
        group="Slides",
        reference_doc=True,
        note="Each level-1/2 heading starts a new slide.",
    ),
    OutputFormat(
        key="revealjs",
        label="HTML slides (reveal.js)",
        ext=".html",
        mime="text/html",
        writer="revealjs",
        group="Slides",
        css=True,
        note="Loads reveal.js from a CDN unless REVEALJS_URL points somewhere local.",
    ),
    # --- web / ebook -------------------------------------------------------
    OutputFormat(
        key="html",
        label="HTML (single file)",
        ext=".html",
        mime="text/html",
        writer="html5",
        group="Web",
        css=True,
        note="Images and styles are embedded, so the file stands alone.",
    ),
    OutputFormat(
        key="html-fragment",
        label="HTML fragment (no <head>)",
        ext=".html",
        mime="text/html",
        writer="html5",
        group="Web",
        standalone=False,
    ),
    OutputFormat(
        key="epub",
        label="EPUB",
        ext=".epub",
        mime="application/epub+zip",
        writer="epub3",
        binary=True,
        group="Web",
        css=True,
    ),
    # --- markup / interchange ---------------------------------------------
    OutputFormat("latex", "LaTeX", ".tex", "application/x-tex", "latex", group="Markup"),
    OutputFormat("typst", "Typst", ".typ", "text/plain", "typst", group="Markup"),
    OutputFormat("gfm", "Markdown (GitHub)", ".md", "text/markdown", "gfm", group="Markup"),
    OutputFormat(
        "commonmark", "Markdown (CommonMark)", ".md", "text/markdown", "commonmark_x", group="Markup"
    ),
    OutputFormat("rst", "reStructuredText", ".rst", "text/x-rst", "rst", group="Markup"),
    OutputFormat("asciidoc", "AsciiDoc", ".adoc", "text/plain", "asciidoc", group="Markup"),
    OutputFormat("org", "Emacs Org-mode", ".org", "text/plain", "org", group="Markup"),
    OutputFormat("mediawiki", "MediaWiki", ".wiki", "text/plain", "mediawiki", group="Markup"),
    OutputFormat("dokuwiki", "DokuWiki", ".txt", "text/plain", "dokuwiki", group="Markup"),
    OutputFormat("textile", "Textile", ".textile", "text/plain", "textile", group="Markup"),
    OutputFormat("man", "man page (roff)", ".1", "text/plain", "man", group="Markup"),
    OutputFormat("texinfo", "Texinfo", ".texi", "text/plain", "texinfo", group="Markup"),
    OutputFormat("docbook", "DocBook XML", ".xml", "application/xml", "docbook5", group="Data"),
    OutputFormat("jats", "JATS XML", ".xml", "application/xml", "jats", group="Data"),
    OutputFormat("opml", "OPML", ".opml", "text/x-opml", "opml", group="Data"),
    OutputFormat("icml", "InDesign ICML", ".icml", "application/xml", "icml", group="Data"),
    OutputFormat("ipynb", "Jupyter notebook", ".ipynb", "application/x-ipynb+json", "ipynb", group="Data"),
    OutputFormat("json", "Pandoc AST (JSON)", ".json", "application/json", "json", group="Data"),
    OutputFormat("plain", "Plain text", ".txt", "text/plain", "plain", group="Data"),
)

FORMATS_BY_KEY: dict[str, OutputFormat] = {f.key: f for f in FORMATS}

PDF_ENGINES: tuple[str, ...] = ("typst", "weasyprint")

#: pandoc's built-in syntax-highlighting styles
HIGHLIGHT_STYLES: tuple[str, ...] = (
    "pygments",
    "tango",
    "espresso",
    "zenburn",
    "kate",
    "monochrome",
    "breezedark",
    "haddock",
    "none",
)

PAPER_SIZES: tuple[str, ...] = ("a4", "letter", "a5", "legal")

#: file extensions we treat as markdown when walking a batch/zip upload
MARKDOWN_SUFFIXES: frozenset[str] = frozenset({".md", ".markdown", ".mdown", ".mkd", ".mdwn", ".text"})


def get_format(key: str) -> OutputFormat:
    try:
        return FORMATS_BY_KEY[key]
    except KeyError:
        raise ValueError(f"unsupported output format: {key!r}") from None
