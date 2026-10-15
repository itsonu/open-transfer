# syntax=docker/dockerfile:1
FROM python:3.14-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE CHANGELOG.md ./
COPY src ./src
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels . \
 && pip wheel --no-cache-dir --wheel-dir /wheels /wheels/*.whl

FROM python:3.14-slim
LABEL org.opencontainers.image.title="Open Transfer" \
      org.opencontainers.image.description="AirDrop-style file sharing for every device on your network" \
      org.opencontainers.image.source="https://github.com/itsonu/open-transfer" \
      org.opencontainers.image.licenses="MIT"
# A server has no owner sitting in front of it: browsers see the shared folder
# (classic mode) and files that apps send to it are accepted automatically.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OPEN_TRANSFER_DIR=/data \
    OPEN_TRANSFER_NO_BROWSER=1 \
    OPEN_TRANSFER_NAME="Open Transfer server" \
    OPEN_TRANSFER_SHARE_FOLDER=1 \
    OPEN_TRANSFER_AUTO_ACCEPT=1
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data && chown app:app /data
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index /wheels/*.whl && rm -rf /wheels
USER app
VOLUME ["/data"]
# 5000/tcp: web + app-to-app HTTP · 47823/udp: nearby-device discovery
EXPOSE 5000 47823/udp
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/api/health', timeout=3)" || exit 1
ENTRYPOINT ["open-transfer"]
