FROM python:3.12-slim-bookworm

# Pin the toolchain so a rebuild produces the same output as the last one.
ARG PANDOC_VERSION=3.6.3
ARG TYPST_VERSION=0.13.1
ARG NODE_VERSION=20.18.1
ARG MERMAID_CLI_VERSION=11.4.2

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PUPPETEER_SKIP_DOWNLOAD=true \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium \
    PUPPETEER_CONFIG=/app/puppeteer-config.json \
    THEMES_DIR=/app/themes \
    XDG_CACHE_HOME=/tmp/cache

# --- system packages -------------------------------------------------------
# WeasyPrint needs pango/cairo/gdk-pixbuf, pandoc's .deb needs libgmp10, and
# mermaid-cli drives chromium.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        xz-utils \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libharfbuzz0b \
        libcairo2 \
        libgdk-pixbuf-2.0-0 \
        libffi8 \
        libgmp10 \
        shared-mime-info \
        fonts-dejavu \
        fonts-liberation2 \
        fonts-noto-core \
        fonts-noto-color-emoji \
        chromium \
    && rm -rf /var/lib/apt/lists/*

# --- node ------------------------------------------------------------------
# From nodejs.org rather than apt: mermaid-cli needs "^18.19 || >=20", which is
# newer than what bookworm guarantees.
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
        amd64) target=x64 ;; \
        arm64) target=arm64 ;; \
        *) echo "unsupported architecture" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/node.tar.xz \
        "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${target}.tar.xz"; \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
        --exclude=CHANGELOG.md --exclude=LICENSE --exclude=README.md; \
    rm /tmp/node.tar.xz; \
    node --version

# --- pandoc ----------------------------------------------------------------
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    curl -fsSL -o /tmp/pandoc.deb \
        "https://github.com/jgm/pandoc/releases/download/${PANDOC_VERSION}/pandoc-${PANDOC_VERSION}-1-${arch}.deb"; \
    dpkg -i /tmp/pandoc.deb; \
    rm /tmp/pandoc.deb; \
    pandoc --version | head -1

# --- typst (the default PDF engine) ---------------------------------------
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
        amd64) target=x86_64-unknown-linux-musl ;; \
        arm64) target=aarch64-unknown-linux-musl ;; \
        *) echo "unsupported architecture" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/typst.tar.xz \
        "https://github.com/typst/typst/releases/download/v${TYPST_VERSION}/typst-${target}.tar.xz"; \
    tar -xJf /tmp/typst.tar.xz -C /tmp; \
    install -m 0755 "/tmp/typst-${target}/typst" /usr/local/bin/typst; \
    rm -rf /tmp/typst.tar.xz "/tmp/typst-${target}"; \
    typst --version

# --- mermaid ---------------------------------------------------------------
RUN npm install -g --omit=dev "@mermaid-js/mermaid-cli@${MERMAID_CLI_VERSION}" \
    && npm cache clean --force

# --- application -----------------------------------------------------------
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY puppeteer-config.json ./
COPY themes ./themes
COPY app ./app

# Chromium and pandoc both want a writable HOME.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /tmp/cache \
    && chown -R appuser:appuser /app /tmp/cache
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
