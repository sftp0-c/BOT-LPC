FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

# API MAX (platform-api2.max.ru) использует сертификаты НУЦ Минцифры, которых нет в образе.
# Ставим корневой и промежуточный сертификаты с портала Госуслуг на этапе сборки.
# Не нужно (свой набор CA / другая сеть) — соберите с --build-arg INSTALL_RU_CA=0.
ARG INSTALL_RU_CA=1
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    if [ "$INSTALL_RU_CA" = "1" ]; then \
        for name in root sub; do \
            file="/usr/local/share/ca-certificates/russian_trusted_${name}_ca.crt"; \
            curl -fsSL "https://gu-st.ru/content/lending/russian_trusted_${name}_ca_pem.crt" -o "$file"; \
            grep -q "BEGIN CERTIFICATE" "$file"; \
        done; \
    fi; \
    update-ca-certificates; \
    apt-get purge -y --auto-remove curl; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY config.py database.py max_api.py bot.py ./

RUN useradd --system --uid 10001 --home-dir /app app \
    && mkdir -p /app/data \
    && chown -R app:app /app/data
USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.getenv('PORT', '8080'), timeout=4)" || exit 1

CMD ["sh", "-c", "exec uvicorn bot:app --host ${HOST:-0.0.0.0} --port ${PORT:-8080}"]
