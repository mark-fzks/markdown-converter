from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings

from .conftest import pandoc_required, typst_required


# --- metadata --------------------------------------------------------------


def test_healthz_is_open(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_formats_endpoint(client):
    body = client.get("/api/formats").json()
    keys = {f["key"] for f in body["formats"]}
    assert {"pdf", "docx", "html", "epub"} <= keys
    assert body["pdf_engines"] == ["typst", "weasyprint"]


def test_themes_endpoint_lists_default(client):
    body = client.get("/api/themes").json()
    assert body["default"] == "default"
    assert any(t["name"] == "default" and t["css"] for t in body["themes"])


def test_index_serves_the_ui(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Markdown Converter" in response.text


# --- auth ------------------------------------------------------------------


@pytest.fixture
def secured_client(env):
    env.setenv("AUTH_USERNAME", "mark")
    env.setenv("AUTH_PASSWORD", "hunter2-and-then-some")
    get_settings.cache_clear()
    from app.main import app

    with TestClient(app) as client:
        yield client


def test_auth_rejects_anonymous(secured_client):
    response = secured_client.get("/api/formats")
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")


def test_auth_rejects_wrong_password(secured_client):
    assert secured_client.get("/api/formats", auth=("mark", "nope")).status_code == 401


def test_auth_accepts_correct_credentials(secured_client):
    assert secured_client.get("/api/formats", auth=("mark", "hunter2-and-then-some")).status_code == 200


def test_healthz_stays_open_with_auth_on(secured_client):
    assert secured_client.get("/healthz").status_code == 200


# --- validation ------------------------------------------------------------


def test_convert_requires_input(client):
    assert client.post("/api/convert", data={"to": "html"}).status_code == 400


def test_convert_rejects_unknown_format(client):
    response = client.post("/api/convert", data={"text": "# hi", "to": "wingdings"})
    assert response.status_code == 400


def test_convert_rejects_unknown_paper_size(client):
    response = client.post("/api/convert", data={"text": "# hi", "to": "pdf", "papersize": "a3"})
    assert response.status_code == 400


def test_convert_rejects_unknown_pdf_engine(client):
    response = client.post(
        "/api/convert", data={"text": "# hi", "to": "pdf", "pdf_engine": "imagination"}
    )
    assert response.status_code == 422


# --- real conversions ------------------------------------------------------


@pandoc_required
def test_convert_text_to_html(client, sample_markdown):
    response = client.post("/api/convert", data={"text": sample_markdown, "to": "html"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "<h1" in body and "Test Document" in body
    # The theme CSS should be inlined, not linked.
    assert "<style>" in body


@pandoc_required
def test_convert_file_to_docx(client, sample_markdown):
    files = {"file": ("notes.md", sample_markdown, "text/markdown")}
    response = client.post("/api/convert", files=files, data={"to": "docx"})

    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="notes.docx"'
    # A .docx is a zip with a known member.
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert "word/document.xml" in zf.namelist()


@pandoc_required
def test_convert_honours_toc_and_numbering(client, sample_markdown):
    response = client.post(
        "/api/convert",
        data={"text": sample_markdown, "to": "html", "toc": "true", "number_sections": "true"},
    )
    assert response.status_code == 200
    assert 'id="TOC"' in response.text
    assert "header-section-number" in response.text


@pandoc_required
def test_convert_to_plain_text(client, sample_markdown):
    response = client.post("/api/convert", data={"text": sample_markdown, "to": "plain"})
    assert response.status_code == 200
    assert "Heading" in response.text
    assert "<h1" not in response.text


@pandoc_required
@typst_required
def test_convert_to_pdf_with_typst(client, sample_markdown):
    response = client.post(
        "/api/convert", data={"text": sample_markdown, "to": "pdf", "pdf_engine": "typst"}
    )
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


@pandoc_required
def test_convert_to_pdf_with_weasyprint(client, sample_markdown):
    pytest.importorskip("weasyprint")
    response = client.post(
        "/api/convert",
        data={"text": sample_markdown, "to": "pdf", "pdf_engine": "weasyprint", "toc": "true"},
    )
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


@pandoc_required
def test_broken_document_returns_422(client):
    # An input format that cannot parse the text should surface as a 4xx, not a 500.
    response = client.post(
        "/api/convert", data={"text": "# hi", "to": "html", "from": "not-a-real-reader"}
    )
    assert response.status_code == 422
    assert "detail" in response.json()


# --- batch -----------------------------------------------------------------


@pandoc_required
def test_batch_of_loose_files_returns_zip(client):
    files = [
        ("files", ("one.md", "# One\n", "text/markdown")),
        ("files", ("two.md", "# Two\n", "text/markdown")),
    ]
    response = client.post("/api/convert/batch", files=files, data={"to": "html"})

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert sorted(zf.namelist()) == ["one.html", "two.html"]


@pandoc_required
def test_batch_from_zip_preserves_folders(client):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("chapter1/intro.md", "# Intro\n")
        zf.writestr("chapter2/body.md", "# Body\n")
        zf.writestr("notes.txt", "ignored")
    buffer.seek(0)

    files = [("files", ("book.zip", buffer.read(), "application/zip"))]
    response = client.post("/api/convert/batch", files=files, data={"to": "html"})

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert sorted(zf.namelist()) == ["chapter1/intro.html", "chapter2/body.html"]


def test_batch_without_markdown_is_rejected(client):
    files = [("files", ("photo.png", b"\x89PNG\r\n", "image/png"))]
    response = client.post("/api/convert/batch", files=files, data={"to": "html"})
    assert response.status_code == 400
