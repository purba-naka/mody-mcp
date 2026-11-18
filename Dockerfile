FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8000

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY server.py ./

RUN useradd -r -u 10001 app
USER app

EXPOSE 8000
CMD ["/app/.venv/bin/python", "server.py"]
