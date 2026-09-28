<#
.SYNOPSIS
    Установка и запуск бота колледжа в MAX на Windows: Docker, .env, сборка, проверка.

.DESCRIPTION
    Скрипт делает всё, что нужно для первого запуска, и проверяет результат:

      1. проверяет Docker и Docker Compose;
      2. создаёт .env из .env.example, если его ещё нет, и генерирует пароль панели;
      3. подсказывает, что вписать вручную (токен бота, MAX ID сис-админа);
      4. собирает и запускает контейнер;
      5. дожидается готовности и печатает адреса и полезные команды.

    Запуск:  .\install.ps1
    Повторный запуск (после правки .env):  .\install.ps1 -Start

.EXAMPLE
    .\install.ps1 -SkipBuild
    Собрать контейнер не нужно - только перезапустить с новым .env.
#>
[CmdletBinding()]
param(
    # Только перезапустить контейнер, не пересобирая образ
    [switch]$Start,
    # Не пересобирать образ при запуске
    [switch]$SkipBuild,
    # Не запускать контейнер в конце (только проверки и .env)
    [switch]$NoStart
)

# В старой консоли Windows PowerShell вывод по умолчанию ANSI - русский текст
# превратился бы в «кракозябры», поэтому включаем UTF-8.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$envFile = Join-Path $root ".env"
$example = Join-Path $root ".env.example"

function Say($text, $color = "Gray") { Write-Host $text -ForegroundColor $color }
function Ok($text) { Say "✔ $text" "Green" }
function Warn($text) { Say "! $text" "Yellow" }
function Die($text) { Say "✖ $text" "Red"; exit 1 }

# ── 1. Docker ───────────────────────────────────────────────────────────────
Say "`n== 1. Проверяем Docker ==" "Cyan"
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Die "Docker не найден. Установите Docker Desktop: https://desktop.docker.com/win/"
}
try { $null = docker version --format "{{.Server.Version}}" 2>&1 } catch { }
if ($LASTEXITCODE -ne 0) {
    Die "Docker Desktop не запущен. Запустите его и повторите."
}
$compose = docker compose version 2>&1
if ($LASTEXITCODE -ne 0) { Die "Не найден docker compose (плагин Docker Desktop)." }
Ok "Docker $(docker version --format '{{.Server.Version}}'), $(($compose -join ' '))"

# ── 2. .env ─────────────────────────────────────────────────────────────────
Say "`n== 2. Настройки (.env) ==" "Cyan"
if ($Start) {
    if (-not (Test-Path $envFile)) { Die "-Start требует существующего .env. Сначала запустите скрипт без ключей." }
    Ok ".env на месте, пропускаем создание"
} elseif (Test-Path $envFile) {
    Ok ".env уже существует - не трогаем (ваш пароль и токен сохранены)"
} else {
    if (-not (Test-Path $example)) { Die "Нет файла .env.example рядом со скриптом." }
    Copy-Item $example $envFile
    $password = -join ((1..20) | ForEach-Object { "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"[(Get-Random -Maximum 57)] })
    (Get-Content $envFile -Raw) -replace "(?m)^WEB_PANEL_PASSWORD=.*$", "WEB_PANEL_PASSWORD=$password" |
        Set-Content $envFile -Encoding UTF8
    Ok "Создан .env, пароль панели сгенерирован: $password"
    Warn "Пароль панели сохраните сами - в .env он, а в панели только в момент входа."
    $token = Read-Host "Токен бота MAX (оставьте пустым, чтобы дописать позже)"
    if ($token) {
        (Get-Content $envFile -Raw) -replace "(?m)^MAX_BOT_TOKEN=.*$", "MAX_BOT_TOKEN=$token" |
            Set-Content $envFile -Encoding UTF8
        Ok "Токен бота записан в .env"
    } else {
        Warn "Токен бота не введён. Пропишите MAX_BOT_TOKEN в .env перед запуском."
    }
    $id = Read-Host "Ваш MAX ID (команда /id в боте) - он станет сис-админом"
    if ($id) {
        (Get-Content $envFile -Raw) -replace "(?m)^SYSADMIN_IDS=.*$", "SYSADMIN_IDS=$id" |
            Set-Content $envFile -Encoding UTF8
        Ok "MAX ID сис-админа записан: $id"
    } else {
        Warn "MAX ID не введён: без него панель не пустит. Пропишите SYSADMIN_IDS в .env."
    }
}

# ── 3. проверка .env ────────────────────────────────────────────────────────
$content = Get-Content $envFile -Raw
if ($content -match "(?m)^MAX_BOT_TOKEN=PASTE") {
    Warn "MAX_BOT_TOKEN в .env не заменён - бот не подключится к MAX."
}
if ($content -match "(?m)^SYSADMIN_IDS=(YOUR_MAX_USER_ID|\s*)$") {
    Warn "SYSADMIN_IDS в .env не заполнен - в панель не войти."
}
if ($content -match "(?m)^WEB_PANEL_PASSWORD=\s*$") {
    Warn "WEB_PANEL_PASSWORD пуст - веб-панель будет выключена."
}

# ── 4. сборка и запуск ──────────────────────────────────────────────────────
if ($NoStart) { Say "`nГотово. Запуск: .\install.ps1 -Start" "Cyan"; exit 0 }

Say "`n== 3. Сборка образа ==" "Cyan"
if ($Start -or $SkipBuild) {
    Ok "сборка пропущена ($-(if ($Start) { 'Start' } else { 'SkipBuild' }))"
} else {
    Push-Location $root
    docker compose build
    if ($LASTEXITCODE -ne 0) { Pop-Location; Die "Сборка не удалась. Подробности выше." }
    Pop-Location
    Ok "образ собран"
}

Say "`n== 4. Запуск контейнера ==" "Cyan"
Push-Location $root
docker compose up -d
if ($LASTEXITCODE -ne 0) { Pop-Location; Die "Не удалось запустить контейнер." }
Pop-Location

# ── 5. здоровье ─────────────────────────────────────────────────────────────
Say "`n== 5. Проверяем, что бот поднялся ==" "Cyan"
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8080/health" -TimeoutSec 3
        if ($health.ok) { $ready = $true; break }
    } catch { }
}
if (-not $ready) {
    Warn "Бот не отвечает на /health. Смотрите журнал: docker compose logs -f bot"
    exit 1
}
Ok "бот отвечает: $($health.platform), режим $($health.mode)"

# ── 6. адреса и что дальше ──────────────────────────────────────────────────
$addresses = @()
try {
    $addresses = Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254*" } |
        Select-Object -ExpandProperty IPAddress
} catch { }

Say "`n== Готово ==" "Green"
Say "Панель сис-админа (вход: ваш MAX ID + пароль из .env):"
foreach ($address in $addresses) { Say "    http://${address}:8080/panel" "White" }
Say "    http://127.0.0.1:8080/panel`n"
Say "Дальше:"
Say "  1. Откройте бота в MAX и нажмите «Начать»."
Say "  2. Напишите /id - он должен совпасть с SYSADMIN_IDS в .env."
Say "  3. В панели заполните сотрудников (должность, роль) и нажмите «Колледж»,"
Say "     чтобы сверить контакты и залить частые вопросы."
Say ""
Say "Полезные команды:"
Say "  docker compose logs -f bot      # журнал"
Say "  docker compose restart bot      # перезапуск без пересборки"
Say "  .\install.ps1 -Start             # применить новый .env"
Say "  .\install.ps1 -NoStart           # только проверки, контейнер не трогать"
