#!/usr/bin/env bash
set -euo pipefail
BASE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export KB_REGISTRY_ROOT="${KB_REGISTRY_ROOT:-/srv/nas/hdd1000/share/Knowledge_Base/Documents}"
export KB_REGISTRY_DB="${KB_REGISTRY_DB:-${BASE_DIR}/data/registry.sqlite3}"
if [[ ! -x "${BASE_DIR}/.venv/bin/python" ]]; then
    printf '%s\n' '실행할 Python이 없습니다. 저장소의 .venv 환경을 생성하고 의존성을 설치하세요.' >&2
    exit 1
fi
exec "${BASE_DIR}/.venv/bin/python" -m kb_registry.server "$@"
