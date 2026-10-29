FROM node:24-bookworm-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm test && npm run build

FROM python:3.13-slim-bookworm AS tests
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/workspace/.venv PATH=/workspace/.venv/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg build-essential pkg-config default-libmysqlclient-dev unixodbc-dev \
    && curl -fsSL https://packages.microsoft.com/config/debian/12/packages-microsoft-prod.deb \
      -o /tmp/packages-microsoft-prod.deb \
    && dpkg -i /tmp/packages-microsoft-prod.deb \
    && apt-get update && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && rm -rf /var/lib/apt/lists/* /tmp/packages-microsoft-prod.deb
RUN pip install --no-cache-dir uv==0.12.20
WORKDIR /workspace
COPY pyproject.toml uv.lock ./
COPY packages/ packages/
RUN uv sync --all-packages --all-extras --all-groups --frozen
COPY examples/testing/requirements.txt /tmp/integration-requirements.txt
RUN uv pip install --python /workspace/.venv/bin/python -r /tmp/integration-requirements.txt
RUN python -m playwright install --with-deps chromium
COPY --from=frontend /packages/redline-core/src/redline_core/ui/static/ packages/redline-core/src/redline_core/ui/static/
COPY examples/ examples/
COPY scripts/ scripts/
COPY docs/ docs/
COPY .github/ .github/
COPY README.md CHANGELOG.md CONTRACT.md RELEASING.md SECURITY.md CONTRIBUTING.md mkdocs.yml ./
CMD ["python", "examples/testing/run.py"]
