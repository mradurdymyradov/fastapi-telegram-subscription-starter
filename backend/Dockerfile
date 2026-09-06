FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=10

WORKDIR /app

COPY pyproject.toml /app/
COPY .wheelhouse /wheelhouse
RUN pip install --upgrade pip \
    && if find /wheelhouse -type f -name '*.whl' | grep -q .; then \
        pip install --no-index --find-links=/wheelhouse .; \
    else \
        pip install .; \
    fi

COPY app /app/app
COPY alembic /app/alembic
COPY alembic.ini /app/

# Run as a dedicated non-root user. Migrations and uploads write to /data
# (mounted volume) and /app stays read-only for the runtime user.
RUN groupadd -r membership_saas --gid=1001 \
    && useradd -r -g membership_saas --uid=1001 --home-dir=/app --shell=/sbin/nologin membership_saas \
    && mkdir -p /data/uploads \
    && chown -R membership_saas:membership_saas /app /data

USER membership_saas:membership_saas

EXPOSE 8000 8080

CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

# --- test stage -------------------------------------------------------------
# Bakes the [dev] extra (pytest, pytest-asyncio, ruff) and the test suite into
# a dedicated image so `pytest` is always runnable. The runtime image (api/bot)
# deliberately does NOT carry pytest; run tests via the `test` compose service:
#   docker compose -p membership_saas --profile test run --rm test
# This stage is NOT the default target — `runtime` below stays last so plain
# `docker compose build` keeps producing the lean runtime image.
FROM base AS test

USER root
RUN if find /wheelhouse -type f -name '*.whl' | grep -q .; then \
        pip install --no-index --find-links=/wheelhouse '.[dev]'; \
    else \
        pip install '.[dev]'; \
    fi
COPY tests /app/tests
RUN chown -R membership_saas:membership_saas /app
USER membership_saas:membership_saas

CMD ["python", "-m", "pytest", "tests", "-q"]

# --- runtime stage (default) ------------------------------------------------
# Intentionally empty: inherits everything from `base`. Kept as the LAST stage
# so services without an explicit `target:` build the runtime image, not test.
FROM base AS runtime
