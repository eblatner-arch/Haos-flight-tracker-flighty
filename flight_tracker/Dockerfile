FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy
RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb x11vnc novnc websockify jq \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir playwright==1.48.0 requests google-genai pydantic
COPY run.py start.sh /
RUN chmod +x /start.sh
CMD ["/start.sh"]
