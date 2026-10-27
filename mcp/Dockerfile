# mcp: the Music MCP adapter (homelab#2412), ghcr.io/sometimeskind/music-mcp
# Build context: mcp/. It reaches only the pipeline's /mcp-api/*, Pocket ID and its own
# Postgres, so it ships none of the pipeline's tools or credentials.
FROM python:3.13-slim AS prod

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

# The package and the guide side by side, as in the source tree (music_mcp.guide.GUIDE_DIR).
COPY music_mcp/ /app/music_mcp/
COPY guide/ /app/guide/
WORKDIR /app

# Non-root; the pod also sets runAsNonRoot and a read-only root filesystem.
USER 65532:65532
EXPOSE 8000 9090
CMD ["python", "-m", "music_mcp"]

FROM prod AS dev
USER root
COPY requirements-dev.txt /tmp/reqs-dev.txt
RUN pip install --no-cache-dir -r /tmp/reqs-dev.txt
COPY pyproject.toml /app/
COPY tests/ /app/tests/
USER 65532:65532
ENTRYPOINT []
CMD ["pytest", "-p", "no:cacheprovider", "tests/"]
