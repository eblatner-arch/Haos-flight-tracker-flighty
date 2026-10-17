FROM python:3.12-slim-bookworm
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb x11vnc novnc websockify jq tzdata fonts-liberation ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf vnc.html /usr/share/novnc/index.html

COPY requirements.txt /tmp/requirements.txt
# Only Chromium (and its system libraries), not the full Playwright image with three browsers.
RUN pip install -r /tmp/requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /tmp/*

COPY rootfs/app /app
COPY run.sh /
RUN chmod +x /run.sh

# Last, so a version bump doesn't invalidate the cached Chromium/apt layers above.
ARG BUILD_VERSION=dev
ENV FT_VERSION=${BUILD_VERSION}

CMD ["/run.sh"]
