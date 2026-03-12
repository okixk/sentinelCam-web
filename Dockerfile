FROM python:3.13-slim
WORKDIR /app

RUN apt-get update && \
    apt-get upgrade -y --no-install-recommends && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd -g 1000 sentinelcam && \
    useradd -u 1000 -g sentinelcam -m sentinelcam

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=sentinelcam:sentinelcam . .
RUN mkdir -p data/recordings && chown -R sentinelcam:sentinelcam data

USER sentinelcam

EXPOSE 3000

HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:3000/health', timeout=5)" || exit 1

CMD ["python", "run_web.py"]
