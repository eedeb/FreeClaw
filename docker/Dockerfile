# FreeClaw — container image (used by the macOS installer)
#
# Mirrors what install.sh does natively on Linux: CPU-only PyTorch, classy-ai,
# sentence-transformers without its dependency tree, then the web/agent libs.
FROM python:3.12-slim

# bash + coreutils are here for the agent's bash tool, not for the build.
# curl/ca-certificates back the search and scraping pipeline.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash \
        coreutils \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./

# download.pytorch.org/whl/cpu only carries x86_64 builds. On arm64 (Apple
# Silicon, which is most Macs now) plain PyPI already resolves to a CPU-only
# aarch64 wheel, so ask for it there instead of 404ing against the CPU index.
RUN if [ "$(uname -m)" = "x86_64" ]; then \
        pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu; \
    else \
        pip install --no-cache-dir torch; \
    fi \
 && pip install --no-cache-dir classy-ai \
 && pip install --no-cache-dir sentence_transformers --no-deps \
 && pip install --no-cache-dir -r requirements.txt

COPY Flask/ ./Flask/
COPY src/ ./src/
COPY models/ ./models/
# Read by src/telemetry.py; .git isn't in the image, so the version has to be
# baked in as a plain file.
COPY VERSION ./

# Both are bind-mounted by docker-compose so data survives a rebuild; creating
# them here keeps the image runnable on its own too.
RUN mkdir -p Flask/static logs

# Where playwright unpacks Chromium for the shipped shadow-web MCP server.
# Pointed at a directory rather than the default ~/.cache so docker-compose can
# mount it: the download is a few hundred MB and lands in the container's
# writable layer, which ./update-mac.sh throws away when it recreates the
# container. Without the mount, every update re-downloads it.
#
# The browser is NOT fetched here. It arrives only if someone switches the
# server on in Settings (src/browser_setup.py), which is also when the apt
# libraries it needs get installed — we're root in this container, so that
# works, and the image stays as small as it was for everyone who doesn't.
ENV PLAYWRIGHT_BROWSERS_PATH=/app/.playwright
RUN mkdir -p /app/.playwright

EXPOSE 6767

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FC_DEBUG=0

# No reloader and no Werkzeug debugger in the container (FC_DEBUG=0). A
# restart from Settings exits the process; compose's restart policy brings it
# straight back, which is the same contract systemd provides on Linux.
CMD ["python3", "-m", "Flask.main"]
