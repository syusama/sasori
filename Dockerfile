ARG PYTHON_BASE=docker.m.daocloud.io/library/python:3.12-slim@sha256:646fb0bca3dd3ea1bcc6feb72c17ed16eed6e10cffc732fcc1478bd3e7f02d7b

FROM ${PYTHON_BASE} AS builder
ARG PYTHON_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_INDEX_URL=${PYTHON_INDEX_URL} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY requirements-build.txt ./
RUN python -m pip install --require-hashes -r requirements-build.txt
COPY pyproject.toml MANIFEST.in README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY licenses ./licenses
COPY src ./src
RUN find src -type d \( -name "*.egg-info" -o -name "__pycache__" \) \
      -prune -exec rm -rf -- {} + \
    && find src -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete \
    && python -m pip wheel --no-build-isolation --no-deps --wheel-dir /wheels .

FROM ${PYTHON_BASE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SASORI_DB=/data/sasori.sqlite3 \
    SASORI_ARTIFACT_ROOT=/data/artifacts \
    SASORI_ACTION_LOG=/data/incident-actions.jsonl
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-deps /wheels/*.whl \
    && rm -rf /wheels \
    && mkdir -p /data \
    && chown 10001:10001 /data
WORKDIR /data
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=2).read()"]
ENTRYPOINT ["sasori-server"]
CMD ["--host", "0.0.0.0", "--port", "8080", "--app", "incident=sasori_apps.incident:create_harness", "--app", "research=sasori_apps.research:create_harness", "--app", "developer=sasori_apps.developer:create_harness", "--token-file", "/run/secrets/sasori_token"]
