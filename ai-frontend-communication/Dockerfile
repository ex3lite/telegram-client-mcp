FROM node:24-alpine AS web
WORKDIR /build/web
RUN corepack enable
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build

FROM ghcr.io/astral-sh/uv:0.11.6 AS uv

FROM python:3.14-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    DISABLE_UPDATES=1 \
    PATH=/app/.venv/bin:$PATH
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git gnupg openssh-client \
    && install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://downloads.claude.ai/keys/claude-code.asc \
      -o /etc/apt/keyrings/claude-code.asc \
    && gpg --show-keys --with-colons /etc/apt/keyrings/claude-code.asc \
      | grep -F "fpr:::::::::31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE:" \
    && echo "deb [signed-by=/etc/apt/keyrings/claude-code.asc] https://downloads.claude.ai/claude-code/apt/stable stable main" \
      > /etc/apt/sources.list.d/claude-code.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends claude-code \
    && claude --version \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
RUN uv sync --frozen --no-dev
COPY alembic.ini ./
COPY migrations/ migrations/
COPY --from=web /build/web/dist web/dist
RUN groupadd --system dca \
    && useradd --system --gid dca --home-dir /app dca \
    && mkdir -p /runtime/repositories /runtime/snapshots \
    && chown -R dca:dca /app /runtime
USER dca
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "dca-api"]
