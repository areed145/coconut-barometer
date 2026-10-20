# The web app, built from uv.lock. Python 3.14: every compiled dependency has a ready-made
# Linux wheel for it (3.15 lacks some), so nothing is compiled during the build.
FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.14 UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app

# Dependencies first (cached until uv.lock changes), then the app itself.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-editable --no-install-project
COPY flickr_organizer ./flickr_organizer
RUN uv sync --locked --no-dev --no-editable

RUN useradd --system --create-home --uid 10001 app
COPY docker/entrypoint.py /entrypoint.py

# Everything that must persist lives on the disk (render.yaml mounts it at /var/data).
ENV PATH="/app/.venv/bin:$PATH" FLICKR_ORGANIZER_DATA=/var/data PYTHONUNBUFFERED=1
EXPOSE 10000
ENTRYPOINT ["python", "/entrypoint.py"]
CMD ["flickr-organizer", "serve"]
