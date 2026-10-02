FROM python:3.12-slim AS base

# uv does the dependency work, python:3.12-slim keeps the image small
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# the app runs as an unprivileged user that owns its files from the first layer on. a
# `chown -R` after the install would copy all of /app, virtualenv included, into a second layer.
# generated/ is a named volume in compose; docker copies this directory's ownership into a
# fresh volume, so the user can write there without an entrypoint chown dance.
RUN useradd --create-home --uid 1000 onboarding \
    && mkdir -p /app/generated \
    && chown -R onboarding:onboarding /app
USER onboarding
WORKDIR /app

# install deps first so code changes do not bust the layer cache. uv's download cache lives in a
# build cache mount, so it speeds up rebuilds without ending up in the image.
COPY --chown=onboarding:onboarding pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/home/onboarding/.cache/uv,uid=1000,gid=1000 \
    uv sync --frozen --no-dev --no-install-project

COPY --chown=onboarding:onboarding . .
RUN --mount=type=cache,target=/home/onboarding/.cache/uv,uid=1000,gid=1000 \
    uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000 8501
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
