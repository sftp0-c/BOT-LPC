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

# Время пользователя: docker-compose.yml задаёт TZ=Europe/Yekaterinburg, а
# tzdata в образе уже есть (apt-get install tzdata отвечает «already the newest
# version»). Проблема в другом: в tzdata 2026c нет обратной ссылки
# Europe/Yekaterinburg - Екатеринбург живёт как Asia/Yekaterinburg, и без этой
# ссылки время в контейнере тихо остаётся в UTC вместо +05:00. Возвращаем
# ссылку, которую ждёт compose.
RUN mkdir -p /usr/share/zoneinfo/Europe \
    && ln -snf ../Asia/Yekaterinburg /usr/share/zoneinfo/Europe/Yekaterinburg

WORKDIR /app

# Зависимости ставятся до кода: правка бота не заставляет пересобирать pip.
COPY requirements.txt .
COPY VERSION ./
RUN pip install -r requirements.txt

# Модули верхнего уровня. Список полный: bot.py импортирует webpanel, а тот -
# repository, timetable, charts, handlers. Забытый здесь модуль = падение на старте.
COPY config.py database.py max_api.py repository.py updates.py utils.py college.py \
     timetable.py charts.py schedule_import.py schedule_watch.py attachments.py version.py \
     panel_theme.py tunnel.py clock.py bot_commands.py webpanel.py bot.py \
     bridge_service.py ./
COPY handlers ./handlers
# Слои данных и панели разложены по папкам: store/ - SQL, web/ - маршруты.
# Забытая здесь папка означала бы падение на первом импорте в контейнере,
# поэтому папки перечислены явно, а tests/test_dockerfile_modules.py
# требует, чтобы каждая папка проекта с __init__.py тут была.
COPY store ./store
COPY web ./web
# Проверяем импорты на этапе сборки: без неё забытый модуль всплыл бы только
# при первом запуске контейнера - в 3 часа ночи.
RUN python -c "import config, database, max_api, repository, updates, utils, college, timetable, charts, schedule_import, schedule_watch, attachments, panel_theme, tunnel, clock, bot_commands, version, webpanel, bridge_service, bot, handlers, store, web; print('импорты в порядке')"
# .dockerignore уже исключает тесты, но папка с данными может быть смонтирована
# в образ при локальной сборке - создаём заранее, чтобы права были верными.
RUN useradd --system --uid 10001 --home-dir /app app \
    && mkdir -p /app/data \
    && chown -R app:app /app/data
USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.getenv('PORT', '8080'), timeout=4)" || exit 1

# Две службы в одном контейнере. Мост - на своём порту, который на хосте
# опубликован только на 127.0.0.1, поэтому из сети и из туннеля эти адреса
# недоступны вообще. Основной бот остаётся ведущей службой: его порт
# открыт наружу, и его проверяет healthcheck.
CMD ["sh", "-c", "uvicorn bridge_service:app --host 0.0.0.0 --port ${BRIDGE_PORT:-8090} & exec uvicorn bot:app --host ${HOST:-0.0.0.0} --port ${PORT:-8080}"]
