# 구현·검증 보고

검증일: 2026-11-21. 기존 서비스·Knowledge_Base 파일은 덮어쓰지 않았습니다.

## 확정한 설계

- 문서당 카테고리 하나 또는 `null`.
- 내부 category ID 유지, 코드 변경 후 증가 번호 연속 사용, 과거 코드 영구 예약.
- 카테고리 논리 삭제와 문서·할당 이력 보존.
- Registry가 ID·메타데이터를 예약하고 Writer가 파일에 적용한 뒤 확인.
- 등록 이후 YAML의 변조된 값으로 DB의 번호 예약·카운터를 갱신하지 않음.
- 명시적 `category: null`만 분류 해제. 필수 인자 누락은 한국어 오류.
- MS-A-2 접근 차단 시 구현을 진행하되 기술자료는 템플릿 미확인 로컬 초안으로 구분하도록 사용자 확인을 받음.

## 신규 파일

| 경로 | 내용 |
| --- | --- |
| `src/kb_registry/registry.py` | 비즈니스 로직·읽기 전용 파일 검사·트랜잭션 |
| `src/kb_registry/schema.sql` | SQLite 초기 스키마·제약 |
| `src/kb_registry/server.py` | FastMCP 도구 13개·한국어 입력 오류 |
| `src/kb_registry/__init__.py` | Python 패키지 |
| `scripts/run.sh` | 기본 경로·DB 설정과 stdio 실행 |
| `pyproject.toml`, `.gitignore` | 의존성·패키징·영속 데이터 제외 |
| `tests/test_registry.py`, `tests/test_stdio.py` | 임시 KB 기반 단위·동시성·stdio 테스트 |
| `examples/opencode.json` | OpenCode V2 로컬 MCP 연결 예시 |
| `README.md` | 설치·운영·Writer 연동 안내 |
| `docs/TECHNICAL_DRAFT.md` | 개념별 원소형 계층 기술자료 초안 |
| `docs/VERIFICATION.md` | 검증 결과·외부 동기화 상태 |

## 검증 결과

- 환경: Python 3.12.16, FastMCP 3.4.11, PyYAML 6.0.4, pytest 9.2.0.
- `python -m pytest -q`: **73 passed**. 초기 58개 통과 후 코드 검토에서 발견한 사항을 수정하고 회귀 테스트를 보강했습니다.
- 스레드/프로세스 동시 발급, 동일 키 프로세스 간 멱등성, 재시작, 기존 ID·표시 번호 선점, 변경·삭제·재할당·코드 변경 이력 검증.
- DB 파일이 없는 최초 8프로세스 동시 초기화에서 WAL 전환 충돌을 재현하고, `fcntl.flock` 초기화 잠금으로 수정했습니다. 전체 테스트 통과 후 해당 테스트를 5회 추가 반복하여 모두 통과했습니다.
- 잘못된 YAML·중복 key·ID/번호 충돌·외부 symlink·경로 이탈·미확정·파일 누락·DB 손상 및 의미적 counter 오류 검증.
- 등록 파일 변조가 Registry 값을 덮어쓰거나 번호 카운터를 오염시키지 않는지 검증.
- 실제 실행 스크립트를 임의의 작업 디렉터리에서 실행하여 MCP 초기화·도구 목록·예약·실패·확인·nullable 카테고리·코드 변경·이동·삭제까지 검증.
- `pip check`, Python compile, `bash -n scripts/run.sh`, `git diff --check` 통과.
- 배포 wheel 빌드 및 `kb_registry/schema.sql` 포함 확인.
- 독립 Reviewer의 최종 검토에서 요청 수정 반영 및 신규 critical/high 결함 없음 확인. 운영 승인이나 게시 완료를 뜻하지 않습니다.

## 실제 기본 경로 읽기 전용 점검

환경변수를 따로 주지 않고 실행 스크립트의 기본 설정으로 stdio 연결했습니다.

- 영속 DB: `data/registry.sqlite3` 초기화. DB·WAL·가상환경 Git 제외 확인.
- 도구 13개 노출 확인.
- 실제 Documents: 47개 Markdown 문서, YAML frontmatter 없음.
- `validate_registry`: `document_count=0`, `file_count=47`, 진단은 `unregistered` 47건. 따라서 `ok=false`는 **아직 문서를 등록하지 않은 예상 상태**이며 오류 은폐나 일괄 발급을 하지 않았습니다.
- 검사 전후 기존 47개 문서의 상대 경로·SHA256 전체 동일 확인. 문서 일괄 등록·수정 없음.
- 서비스 경로의 파일시스템 종류는 `ext2/ext3`로 확인됐으며 공유 네트워크 DB 다중 호스트 운영은 검증하지 않았습니다.

## 외부 동기화·남은 항목

- GitHub: Git CLI HTTPS·SSH 인증은 없었으므로 인증된 GitHub MCP를 통한 업로드를 사용합니다. 실제 완료 commit과 원격 일치 결과는 작업 완료 보고에서 확인합니다. 실행 비트에 의존하지 않도록 연결 예시와 테스트는 `bash scripts/run.sh`를 사용합니다.
- YouTrack: 첫 조회에서 HTTP 429·15분 lockout. 재시도·우회하지 않았으며 MS-A-2 템플릿 및 MKR 프로젝트 확인 전에는 게시 완료로 간주하지 않습니다.
- `TECHNICAL_DRAFT.md`는 템플릿 적용 전 초안입니다. 템플릿 확인과 MKR의 원소형 계층 자료 생성은 별도 결과 확인이 필요합니다.
- OpenCode 예시는 제공했지만 실제 사용자 설정을 자동 변경하거나 서버를 전역 등록하지 않았습니다.
- 기존 Writer 설정이 `knowledge/**`를 가정하는 부분은 운영자가 실제 `Documents/**`와 권한 정책을 검토해야 합니다.
- 커밋된 코드에는 운영 DB·개인 문서 내용·비밀값을 포함하지 않습니다.
