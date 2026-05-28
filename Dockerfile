ARG PYTHON_VERSION=3.14
FROM python:${PYTHON_VERSION}-slim
WORKDIR /app

# Apache sits in front of uvicorn inside this container:
#   :80 (apache) -> 127.0.0.1:3000 (uvicorn)
# Apache also serves /static/ directly without proxying. TLS is terminated
# upstream by Traefik, so Apache here only speaks plain HTTP.
RUN apt-get update && \
    apt-get upgrade -y --no-install-recommends && \
    apt-get install -y --no-install-recommends \
        libglib2.0-0 ffmpeg \
        apache2 supervisor && \
    a2enmod proxy proxy_http proxy_wstunnel rewrite headers expires && \
    a2dissite 000-default && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd -g 1000 sentinelcam && \
    useradd -u 1000 -g sentinelcam -m sentinelcam && \
    mkdir -p /data/recordings /var/log/supervisor && \
    chown -R sentinelcam:sentinelcam /data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY web-apache/sentinelcam.conf /etc/apache2/sites-available/sentinelcam.conf
COPY web-apache/supervisord.conf /etc/supervisor/conf.d/sentinelcam.conf
RUN a2ensite sentinelcam

COPY --chown=sentinelcam:sentinelcam . .

EXPOSE 80

HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost/healthz', timeout=5)" || exit 1

CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
