FROM python:3.12-slim
# Numeric UID/GID 10001 so runAsNonRoot can be verified (PSS restricted).
RUN groupadd -r -g 10001 agentuser && useradd -r -u 10001 -g agentuser -d /app agentuser

WORKDIR /app

# Proxy build args (passed via --build-arg; needed for pip behind a proxy)
ARG http_proxy
ARG https_proxy
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG no_proxy
ARG NO_PROXY

COPY requirements.txt .
# apt-get upgrade patches base-image OS packages flagged by the image scan.
# No compiler: every dependency ships a wheel.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --upgrade "pip>=26.2" \
    && pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY app/ app/
COPY frontend/ frontend/

# The root filesystem is read-only in Kubernetes; /tmp is an emptyDir.
ENV HOME=/tmp \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 10001:10001

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=5)"

CMD ["python", "main.py"]
