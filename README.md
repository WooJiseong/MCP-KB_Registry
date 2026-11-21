# Knowledge Base Registry MCP

Python 3.12 · FastMCP · SQLite 기반 Linux용 독립적인 로컬 stdio 서버입니다. 문서 ID·카테고리 번호·경로·YouTrack Issue 관계를 관리하며 **문서 파일은 읽기만 합니다**. systemd, HTTP 포트, ContextForge 등록은 필요하지 않습니다.

## 설치와 실행

```bash
cd /srv/nas/hdd1000/share/services/KB_Registry
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
bash scripts/run.sh
```

stdio 서버이므로 터미널에서 대화형 명령 프롬프트가 나타나지 않는 것이 정상입니다. 실제 사용 시 MCP 클라이언트가 실행 스크립트를 호출합니다. stdout에 일반 메시지를 출력하거나 셸 초기화 메시지를 추가하지 마세요.

| 환경 변수 | 기본값 |
| --- | --- |
| `KB_REGISTRY_ROOT` | `/srv/nas/hdd1000/share/Knowledge_Base/Documents` |
| `KB_REGISTRY_DB` | 서비스 디렉터리의 `data/registry.sqlite3` |

DB와 WAL/SHM·초기화 잠금 파일, 가상환경은 Git에서 제외합니다. DB는 영속 경로에 두고 SQLite 잠금이 정상인 **동일 호스트의 로컬 파일시스템**에서 사용하세요. 여러 호스트의 NFS/SMB 공유 DB 운영은 보장하지 않습니다. 하나의 DB는 하나의 정규화된 KB 루트에 대응합니다.

최초 WAL 전환·스키마 초기화는 표준 라이브러리 `fcntl.flock`으로 직렬화하고, 일반 쓰기는 SQLite `BEGIN IMMEDIATE`와 `busy_timeout`으로 보호합니다. DB 옆의 `.init.lock` 파일은 잠금 inode를 유지하기 위해 삭제하지 않습니다. 파일 존재 자체는 잠김을 뜻하지 않으며 프로세스 종료 시 커널 잠금은 자동 해제됩니다. 초기화 잠금 대기는 시간 제한이 없으므로 시작이 멈춘 경우 파일을 지우지 말고 기존 프로세스의 정지 여부를 점검하세요.

## 책임과 데이터 계약

| 항목 | 기준 저장소·담당 |
| --- | --- |
| 불변 `KB-000001` ID, 분류 번호, 경로, Issue 매핑 | SQLite Registry |
| 제목, Markdown 본문, LaTeX, 사용자 YAML 필드 | 문서 파일·Writer |
| Registry 소유 YAML 필드 반영 | Registry 반환값을 받은 Writer |
| 실제 YouTrack 생성·수정·존재 확인 | 별도 Tracker |

Registry가 반환하는 `expected_frontmatter`의 다음 필드를 문서 YAML에 병합합니다. 아래는 **형식 예시이며 실제 문서에 이 값을 복사하거나 ID를 직접 생성하면 안 됩니다**.

```yaml
id: KB-000001
category: RL
category_number: RL-001
youtrack_issues: []
```

- `category`와 `category_number`는 함께 `null`일 수 있습니다.
- 관련 Issue는 여러 개이며 중복 없이 정렬합니다. 원격 YouTrack API는 호출하지 않습니다.
- `.md`, `.markdown`, `.qmd` 문서를 지원하며 본문·수식은 파싱하거나 변경하지 않습니다.
- 제목·파일명·디렉터리 구조로 ID를 계산하지 않습니다. 디렉터리를 카테고리 구조에 맞출 필요도 없습니다.
- 등록 후에는 YAML 값이 DB 값을 덮어쓰지 않습니다. 불일치는 확인·복구 대상으로 반환합니다.

## 도구

| 도구 | 역할 |
| --- | --- |
| `allocate_document` | 경로와 선택적 카테고리·멱등성 키로 예약, 기존 ID 가져오기 |
| `confirm_document` | 실제 파일과 메타데이터 일치 검사 후 등록 확정 |
| `get_document` | KB ID로 현재 정보·기대 YAML 조회 |
| `assign_category` | 카테고리와 표시 번호 할당; `null`은 해제 |
| `remove_category` | 현재 카테고리 해제 |
| `link_youtrack` | 문서에 확인된 Issue ID 연결 |
| `list_categories` | 카테고리 목록 조회 |
| `create_category` | 선택적 설명 이름과 함께 카테고리 사전 생성 |
| `validate_registry` | DB 및 문서 전체 무결성·동기화 검사 |
| `reconcile_document` | 불일치와 Writer용 복구 제안, 파일 변경 없음 |
| `update_document_path` | 이동한 문서의 경로 매핑 갱신 |
| `rename_category` | 코드 변경, 안정적인 내부 카테고리는 유지 |
| `delete_category` | 논리 삭제, 문서는 유지·현재 카테고리는 해제 |

정확한 인자·반환 스키마는 MCP `tools/list`와 `src/kb_registry/server.py`를 기준으로 합니다. 문서 응답에는 `kb_id`, `path`, `status`, `expected_frontmatter`가 포함됩니다.

### 카테고리 정책

문서에는 카테고리 하나 또는 `null`을 지정합니다. 새 카테고리는 유효한 코드를 처음 사용하면 만들어집니다. 표시 번호는 카테고리별로 증가하며 **해제·이동·삭제 후에도 재사용하지 않습니다**.

카테고리 코드는 대문자로 시작하는 영문 대문자·숫자 1~16자입니다. 설명용 이름은 별도로 지정할 수 있습니다. `list_categories`에는 삭제 상태·내부 ID·과거 코드 alias도 포함됩니다.

- 같은 현재 카테고리의 반복 지정은 같은 번호를 반환합니다.
- 해제 후 재지정은 새로운 할당이므로 새 번호입니다.
- 카테고리 코드 변경은 내부 카테고리를 유지하고 증가 번호를 이어갑니다.
- 이전 코드는 영구 예약합니다. 삭제한 카테고리·번호·이력은 보존합니다.
- 카테고리 삭제로 문서 자체를 삭제하지 않습니다.

## Writer Agent 연동

### 새 문서

1. 기존 문서를 검색합니다. 제목이 같다는 이유만으로 자동 병합하지 않습니다.
2. `allocate_document(path="원하는/문서.md", category="RL", idempotency_key="작업별-고유키")`를 호출합니다.
3. 반환된 `expected_frontmatter`만 문서에 병합하고 사용자 필드·본문·LaTeX는 보존합니다. 파일 수정 권한은 Writer에만 줍니다.
4. `confirm_document(kb_id=...)`를 호출합니다. 성공 전에는 등록 완료로 보고하지 않습니다.
5. 필요하면 Tracker가 확인한 Issue ID를 `link_youtrack`으로 연결하고, 최신 YAML을 반영한 뒤 재확인합니다.

예약과 파일 작성은 단일 트랜잭션이 아닙니다. 파일 쓰기 실패 시 Pending을 유지하고 같은 경로·같은 멱등성 키로 재시도하세요. 키를 다른 경로에 재사용하면 충돌입니다. Registry 사용 불가 시 ID 없는 초안을 남기고 차단을 보고하며 번호를 추측하지 않습니다.

이미 예약된 경로·키의 `allocate_document`는 현재 문서를 반환하며 category 인자를 다르게 보내도 분류를 변경하지 않습니다. 변경은 `assign_category`로 요청하세요. 카테고리 해제는 `category: null`을 명시해야 하며 인자 누락은 오류입니다.

### 기존 문서·이동

- 기존 파일 경로로 예약하면 파일의 ID를 검사합니다. 중복 ID·표시 번호 충돌을 거부하고 ID가 없는 파일만 새로 발급합니다.
- 미등록 문서에 이미 있는 ID·번호도 스캔하여 충돌을 방지합니다. 손상된 YAML은 먼저 복구하세요.
- 파일 이동은 Writer가 기존 ID를 유지해 수행한 후 `update_document_path`로 새 위치를 등록합니다. 같은 ID의 원본과 복사본을 동시에 두지 마세요.
- 카테고리·Issue·경로 변경 후에는 최신 기대 YAML을 반영하고 다시 확인합니다.
- `reconcile_document` 결과는 **제안**입니다. Registry가 파일을 수정하거나 모든 충돌을 자동 해결하지 않습니다.

기존 KB의 `knowledge/**` 경로를 가정하는 Writer 권한 설정은 실제 `Documents/**` 정책과 다를 수 있습니다. 이 프로젝트는 기존 Agent 설정을 자동 변경하지 않습니다. 사용 시 운영자가 Writer의 실제 문서 편집 권한과 MCP 호출 권한을 검토해야 합니다.

## OpenCode 연결

[`examples/opencode.json`](examples/opencode.json)은 공식 OpenCode V2 `mcp.servers` 형식입니다. 기존 설정에 `kb-registry` 항목을 **병합**하고 다른 서버·권한 설정은 보존하세요. 예시 파일을 전역/프로젝트 설정에 자동 설치하지 않습니다.

```bash
opencode mcp add kb-registry -- bash /srv/nas/hdd1000/share/services/KB_Registry/scripts/run.sh
opencode mcp list
```

공식 참조: <https://opencode.ai/v2/docs/mcp-servers>

## 검증과 운영

```bash
.venv/bin/python -m pytest -q
```

테스트는 임시 KB·임시 DB만 사용하며 실제 Documents를 수정하지 않습니다. 프로세스 간 동시 발급, 예약 재시도, 카테고리 이력, 이동, 기존 ID, YAML 오류, 경로 이탈, Issue 중복 및 MCP stdio 초기화·호출을 검증합니다. 실제 결과는 [`docs/VERIFICATION.md`](docs/VERIFICATION.md)를 확인하세요.

- `validate_registry`는 읽기 전용 검사입니다. 미등록·Pending 문서가 있으면 정상 완료로 간주하지 않습니다.
- 스캔 비용은 문서 수에 비례합니다. 대규모 KB용 외부 인덱스·파일 감시는 도입하지 않았습니다.
- 확인 직후 외부 Writer가 파일을 변경할 수 있습니다. Committed는 확인 시점의 일치이며 영구적인 파일 잠금이 아닙니다.
- OS 권한으로 KB·DB·부모 디렉터리를 보호하세요. 로컬 stdio는 다중 사용자 인증 서비스가 아닙니다.
- 외부 symlink 파일을 거부하며 symlink 디렉터리는 내부를 가리키더라도 스캔하지 않고 안전 실패합니다.
- 백업은 서버를 중단하거나 SQLite backup API를 사용하세요. 활성 WAL 모드에서 DB 파일만 복사하면 최신 변경이 빠질 수 있습니다.
- DB를 삭제·초기화하면 과거 번호 예약이 사라집니다. 임의 초기화 대신 검증·백업 복원을 우선합니다.
- 기술 개념별 초안은 [`docs/TECHNICAL_DRAFT.md`](docs/TECHNICAL_DRAFT.md)입니다. MS-A-2 템플릿 확인·MKR 동기화 여부는 검증 보고서에 별도 기록합니다.
