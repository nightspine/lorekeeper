# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.12-slim AS build
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra web --extra claude
COPY src/ src/
COPY schema/ schema/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra web --extra claude

FROM python:3.12-slim
# The project is installed editable from /app/src, so this path must match the build stage.
# The app reads SPEC.md and README.md from the repo root, examples/ is the bundled demo library,
# and site/ is the landing page.
WORKDIR /app
RUN useradd --system --uid 10001 --home-dir /app lorekeeper \
    && mkdir /data && chown lorekeeper /data
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/src /app/src
COPY pyproject.toml README.md SPEC.md ./
COPY schema/ schema/
COPY examples/ examples/
COPY site/ site/
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 LOREKEEPER_DATA=/data
USER lorekeeper
VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/.well-known/lorekeeper', timeout=4)"
ENTRYPOINT ["lorekeeper", "web", "--host", "0.0.0.0", "--port", "8765", "--data", "/data"]
