FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy
ARG BUILD_VERSION=dev
ENV FT_VERSION=${BUILD_VERSION}

RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb x11vnc novnc websockify jq tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY rootfs/app /app
COPY run.sh /
RUN chmod +x /run.sh

CMD ["/run.sh"]
