# =============================================================================
# Multi-stage build.
#
# Stage 1 installs the dependencies into a virtual environment; stage 2 copies
# only that environment and the application, so compilers, build caches and
# package metadata never reach the published image.
#
# The container runs as a non-root user. A migration tool has no reason to hold
# root inside its own container, and an image that does is the first thing a
# security review flags.
# =============================================================================

# ---------- Stage 1: builder -------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Dependency metadata first: this layer stays cached as long as pyproject.toml
# is unchanged, so editing a mapping does not reinstall SQLAlchemy.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --upgrade pip setuptools wheel && pip install .

# ---------- Stage 2: runtime -------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="crm-erp-data-migration" \
      org.opencontainers.image.description="Declarative CRM migration: YAML mappings, mandatory dry-run, batched loading into a Dataverse-style API." \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/legend-cell05/crm-erp-data-migration"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    KEYSTONE_PROJECT_ROOT=/app \
    KEYSTONE_LOG_FORMAT=json

# psql is used by the Makefile targets and is worth its few megabytes the
# first time something has to be debugged from inside the container.
RUN apt-get update \
    && apt-get install --no-install-recommends -y postgresql-client curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash --uid 10001 keystone

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
# The mappings and the SQL are configuration, not code: they are copied as
# files so that a mapping change is a file change, reviewable in a diff.
COPY --chown=keystone:keystone sql ./sql
COPY --chown=keystone:keystone mappings ./mappings
COPY --chown=keystone:keystone README.md LICENSE pyproject.toml ./
RUN mkdir -p /app/data/legacy_exports /app/data/reports && chown -R keystone:keystone /app/data

USER keystone

# `keystone doctor` already exits non-zero when the database is unreachable,
# which is exactly the semantics a healthcheck needs.
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD keystone doctor > /dev/null 2>&1 || exit 1

ENTRYPOINT ["keystone"]
CMD ["--help"]
