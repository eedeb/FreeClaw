# FreeClaw — container image (used by the macOS installer)
#
# Mirrors what install.sh does natively on Linux: the web/agent libs, then the
# NLTK word table the intent classifier tokenises with.
FROM python:3.12-slim

# bash + coreutils are here for the agent's bash tool, not for the build.
# curl/ca-certificates back the search and scraping pipeline.
#
# xvfb is the virtual display the *sign-in* browser needs (see
# src/browser_takeover.py). Baked into the image rather than fetched when the
# browser server is switched on, unlike Chromium itself: it's tens of MB
# against Chromium's few hundred, and the runtime path can only work while apt
# still has package lists — which the cleanup on the last line of this very
# command deletes. Without it that browser falls back to headless, and headless
# is the one thing Google and Microsoft sign-in refuse.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash \
        coreutils \
        curl \
        ca-certificates \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./

# punkt_tab is baked in rather than fetched at runtime: models/run_model.py
# asks for it on import, and a container that starts without network would
# otherwise lose the classifier on every turn. It is a few MB.
RUN pip install --no-cache-dir -r requirements.txt \
 && python -c "import nltk; nltk.download('punkt_tab', quiet=True)"

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
