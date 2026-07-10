#!/usr/bin/env bash
# scripts/smoke-test.sh — прогон doctor.sh в Linux-контейнере.
# Запускается с Windows/macOS/Linux, где установлен Docker. Проверяет, что
# POSIX-путь run_cmd (/bin/sh -c), блок-листы и изоляция работают на практике.
# Не покрывает brew/paths/pbcopy/Tailscale — только серверную и doctor-логику.

set -euo pipefail

# Скрипт лежит в scripts/, репо — на уровень выше.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

IMAGE_TAG="repopilot-smoke:latest"
CONTAINER_NAME="repopilot-smoke-$$"

log()  { printf '\033[36m[smoke]\033[0m %s\n' "$1"; }
ok()   { printf '\033[32m[smoke]\033[0m %s\n' "$1"; }
err()  { printf '\033[31m[smoke]\033[0m %s\n' "$1" >&2; }

if ! command -v docker >/dev/null 2>&1; then
    err "Docker не найден. Установите Docker Desktop и запустите снова."
    exit 1
fi

cd "$REPO_DIR"

log "Собираю образ $IMAGE_TAG ..."
if ! docker build -f Dockerfile.smoke -t "$IMAGE_TAG" . ; then
    err "Сборка образа провалилась."
    exit 1
fi

ok "Образ собран. Запускаю doctor.sh в контейнере $CONTAINER_NAME ..."
echo ""

# Прогон doctor.sh. Передаём --rm для авто-очистки. Код выхода пробрасывается.
set +e
docker run --rm --name "$CONTAINER_NAME" "$IMAGE_TAG"
RC=$?
set -e

echo ""
if [ "$RC" = 0 ]; then
    ok "SMOKE TEST ПРОЙДЕН (doctor.sh вернул 0)"
    exit 0
else
    err "SMOKE TEST ПРОВАЛЕН (doctor.sh вернул $RC)"
    err "См. логи выше. Для отладки: docker run --rm -it $IMAGE_TAG bash"
    exit "$RC"
fi
