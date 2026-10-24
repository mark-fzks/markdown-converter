"""FastAPI application: a small self-hosted markdown conversion service."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Iterable

import anyio
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from .archive import ArchiveError, build_zip, safe_extract
from .auth import require_auth, warn_if_open
from .config import Settings, get_settings
from .convert import (
    ConversionError,
    ConvertOptions,
    convert_file,
    sanitize_filename,
    tool_versions,
)
from .formats import (
    FORMATS,
    HIGHLIGHT_STYLES,
    MARKDOWN_SUFFIXES,
    PAPER_SIZES,
    PDF_ENGINES,
    get_format,
)
from .themes import list_themes

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("markdown-converter")

STATIC_DIR = Path(__file__).parent / "static"

_limiter: asyncio.Semaphore | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _limiter
    settings = get_settings()
    warn_if_open(settings)
    _limiter = asyncio.Semaphore(max(1, settings.max_concurrent_conversions))
    log.info(
        "ready: pdf engine=%s, themes=%s, mermaid=%s",
        settings.default_pdf_engine,
        settings.themes_dir,
        "on" if settings.mermaid_enabled else "off",
    )
    yield


app = FastAPI(
    title="Markdown Converter",
    description="Convert markdown to PDF, Word and ~25 other formats.",
    version="1.0.0",
    lifespan=lifespan,
)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def limit_upload_size(request: Request, call_next):
    settings = get_settings()
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > settings.max_upload_bytes:
        return JSONResponse(
            {"detail": f"upload exceeds the {settings.max_upload_bytes // (1024 * 1024)} MB limit"},
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )
    return await call_next(request)


@app.exception_handler(ConversionError)
async def conversion_error_handler(request: Request, exc: ConversionError):
    return JSONResponse({"detail": str(exc)}, status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)


# --------------------------------------------------------------------------
# metadata endpoints
# --------------------------------------------------------------------------


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index(_: Annotated[str | None, Depends(require_auth)]) -> HTMLResponse:
    page = STATIC_DIR / "index.html"
    if not page.is_file():
        return HTMLResponse("<h1>Markdown Converter</h1><p>See <a href='/docs'>/docs</a>.</p>")
    return HTMLResponse(page.read_text(encoding="utf-8"))


@app.get("/api/formats")
async def api_formats(_: Annotated[str | None, Depends(require_auth)]) -> dict[str, object]:
    return {
        "formats": [
            {
                "key": f.key,
                "label": f.label,
                "extension": f.ext,
                "group": f.group,
                "note": f.note,
                "supports_theme_css": f.css,
                "supports_reference_doc": f.reference_doc,
            }
            for f in FORMATS
        ],
        "pdf_engines": list(PDF_ENGINES),
        "paper_sizes": list(PAPER_SIZES),
        "highlight_styles": list(HIGHLIGHT_STYLES),
    }


@app.get("/api/themes")
async def api_themes(
    _: Annotated[str | None, Depends(require_auth)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    return {
        "themes": [theme.as_dict() for theme in list_themes(settings)],
        "default": settings.default_theme,
    }


@app.get("/api/version")
async def api_version(
    _: Annotated[str | None, Depends(require_auth)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    return {"service": app.version, "tools": await anyio.to_thread.run_sync(tool_versions, settings)}


# --------------------------------------------------------------------------
# conversion endpoints
# --------------------------------------------------------------------------


def _options_from_form(
    to: str,
    pdf_engine: str | None,
    theme: str | None,
    toc: bool,
    toc_depth: int,
    number_sections: bool,
    papersize: str,
    fontsize: str,
    margin: str,
    mainfont: str | None,
    highlight_style: str,
    mermaid: bool,
    from_format: str,
    title: str | None,
    author: str | None,
) -> ConvertOptions:
    try:
        get_format(to)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    if papersize not in PAPER_SIZES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown paper size: {papersize!r}")
    if highlight_style not in HIGHLIGHT_STYLES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"unknown highlight style: {highlight_style!r}"
        )

    metadata: dict[str, str] = {}
    if title:
        metadata["title"] = title
    if author:
        metadata["author"] = author

    return ConvertOptions(
        to=to,
        from_format=from_format,
        pdf_engine=pdf_engine or None,
        theme=theme or None,
        toc=toc,
        toc_depth=toc_depth,
        number_sections=number_sections,
        papersize=papersize,
        fontsize=fontsize,
        margin=margin,
        mainfont=mainfont or None,
        highlight_style=highlight_style,
        mermaid=mermaid,
        metadata=metadata,
    )


async def _run_conversion(source: Path, options: ConvertOptions, settings: Settings, out_dir: Path):
    assert _limiter is not None, "application did not start up cleanly"
    async with _limiter:
        return await anyio.to_thread.run_sync(convert_file, source, options, settings, out_dir)


@app.post(
    "/api/convert",
    summary="Convert a single markdown document",
    response_class=FileResponse,
)
async def api_convert(
    _: Annotated[str | None, Depends(require_auth)],
    settings: Annotated[Settings, Depends(get_settings)],
    file: Annotated[UploadFile | None, File(description="A markdown file")] = None,
    text: Annotated[str | None, Form(description="Markdown source, instead of a file")] = None,
    to: Annotated[str, Form()] = "pdf",
    pdf_engine: Annotated[str | None, Form()] = None,
    theme: Annotated[str | None, Form()] = None,
    toc: Annotated[bool, Form()] = False,
    toc_depth: Annotated[int, Form()] = 3,
    number_sections: Annotated[bool, Form()] = False,
    papersize: Annotated[str, Form()] = "a4",
    fontsize: Annotated[str, Form()] = "11pt",
    margin: Annotated[str, Form()] = "2cm",
    mainfont: Annotated[str | None, Form()] = None,
    highlight_style: Annotated[str, Form()] = "pygments",
    mermaid: Annotated[bool, Form()] = True,
    from_format: Annotated[str, Form(alias="from")] = "markdown",
    title: Annotated[str | None, Form()] = None,
    author: Annotated[str | None, Form()] = None,
    filename: Annotated[str | None, Form(description="Base name for the result")] = None,
):
    if file is None and not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "provide either 'file' or 'text'")

    options = _options_from_form(
        to, pdf_engine, theme, toc, toc_depth, number_sections, papersize,
        fontsize, margin, mainfont, highlight_style, mermaid, from_format, title, author,
    )

    workdir = Path(tempfile.mkdtemp(prefix="mdconv-"))
    cleanup = BackgroundTask(shutil.rmtree, workdir, ignore_errors=True)
    try:
        base = filename or (file.filename if file else None) or "document"
        stem = Path(sanitize_filename(base)).stem or "document"
        source = workdir / f"{stem}.md"

        if file is not None:
            await _save_upload(file, source, settings)
        else:
            source.write_text(text or "", encoding="utf-8")

        out_dir = workdir / "out"
        outcome = await _run_conversion(source, options, settings, out_dir)
    except Exception:
        # Nothing will be streamed, so the background cleanup never runs.
        shutil.rmtree(workdir, ignore_errors=True)
        raise

    fmt = options.output_format
    headers = {"Content-Disposition": f'attachment; filename="{stem}{fmt.ext}"'}
    if outcome.warnings:
        headers["X-Conversion-Warnings"] = "; ".join(outcome.warnings)[:900]

    return FileResponse(
        outcome.path,
        media_type=fmt.mime,
        filename=f"{stem}{fmt.ext}",
        headers=headers,
        background=cleanup,
    )


@app.post(
    "/api/convert/batch",
    summary="Convert many markdown files (or a zip) and get a zip back",
    response_class=FileResponse,
)
async def api_convert_batch(
    _: Annotated[str | None, Depends(require_auth)],
    settings: Annotated[Settings, Depends(get_settings)],
    files: Annotated[list[UploadFile], File(description=".md files, or a .zip of them")],
    to: Annotated[str, Form()] = "pdf",
    pdf_engine: Annotated[str | None, Form()] = None,
    theme: Annotated[str | None, Form()] = None,
    toc: Annotated[bool, Form()] = False,
    toc_depth: Annotated[int, Form()] = 3,
    number_sections: Annotated[bool, Form()] = False,
    papersize: Annotated[str, Form()] = "a4",
    fontsize: Annotated[str, Form()] = "11pt",
    margin: Annotated[str, Form()] = "2cm",
    mainfont: Annotated[str | None, Form()] = None,
    highlight_style: Annotated[str, Form()] = "pygments",
    mermaid: Annotated[bool, Form()] = True,
    from_format: Annotated[str, Form(alias="from")] = "markdown",
    filename: Annotated[str | None, Form()] = None,
):
    if not files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no files uploaded")

    options = _options_from_form(
        to, pdf_engine, theme, toc, toc_depth, number_sections, papersize,
        fontsize, margin, mainfont, highlight_style, mermaid, from_format, None, None,
    )

    workdir = Path(tempfile.mkdtemp(prefix="mdconv-batch-"))
    cleanup = BackgroundTask(shutil.rmtree, workdir, ignore_errors=True)
    try:
        inputs = workdir / "in"
        inputs.mkdir()
        await _unpack_batch(files, inputs, settings)

        sources = sorted(_find_markdown(inputs))
        if not sources:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "no markdown files found in the upload"
            )
        if len(sources) > settings.max_batch_files:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"too many markdown files ({len(sources)} > {settings.max_batch_files})",
            )

        results: list[tuple[str, Path]] = []
        failures: list[str] = []
        for source in sources:
            relative = source.relative_to(inputs)
            out_dir = workdir / "out" / relative.parent
            try:
                outcome = await _run_conversion(source, options, settings, out_dir)
            except ConversionError as exc:
                failures.append(f"{relative.as_posix()}: {exc}".replace("\n", " ")[:300])
                continue
            arcname = (relative.parent / outcome.path.name).as_posix()
            results.append((arcname, outcome.path))

        if not results:
            raise ConversionError(
                "every document failed to convert. " + (failures[0] if failures else "")
            )

        if failures:
            report = workdir / "conversion-errors.txt"
            report.write_text("\n".join(failures), encoding="utf-8")
            results.append(("conversion-errors.txt", report))

        stem = Path(sanitize_filename(filename or "converted")).stem or "converted"
        bundle = build_zip(results, workdir / f"{stem}.zip")
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise

    headers = {"Content-Disposition": f'attachment; filename="{stem}.zip"'}
    if failures:
        headers["X-Conversion-Warnings"] = f"{len(failures)} document(s) failed; see conversion-errors.txt"

    return FileResponse(
        bundle,
        media_type="application/zip",
        filename=f"{stem}.zip",
        headers=headers,
        background=cleanup,
    )


# --------------------------------------------------------------------------
# upload helpers
# --------------------------------------------------------------------------


async def _save_upload(upload: UploadFile, target: Path, settings: Settings) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with target.open("wb") as out:
        while chunk := await upload.read(1024 * 1024):
            written += len(chunk)
            if written > settings.max_upload_bytes:
                raise HTTPException(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "upload exceeds the size limit"
                )
            out.write(chunk)
    return target


async def _unpack_batch(files: list[UploadFile], inputs: Path, settings: Settings) -> None:
    for upload in files:
        name = sanitize_filename(upload.filename or "upload")
        staged = inputs / name
        if name.lower().endswith(".zip"):
            staged = inputs.parent / f"_upload-{name}"
            await _save_upload(upload, staged, settings)
            try:
                safe_extract(
                    staged,
                    inputs,
                    max_bytes=settings.max_unpacked_bytes,
                    max_files=settings.max_batch_files * 10,
                )
            except ArchiveError as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
            finally:
                staged.unlink(missing_ok=True)
        else:
            await _save_upload(upload, staged, settings)


def _find_markdown(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in MARKDOWN_SUFFIXES:
            yield path
