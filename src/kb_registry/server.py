"""FastMCP stdio 어댑터. 비즈니스 규칙은 Registry에만 둔다."""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError, ValidationError as FastMCPValidationError
from fastmcp.server.middleware import Middleware, MiddlewareContext, CallNext
from pydantic import ValidationError as PydanticValidationError

from .registry import Registry, RegistryError


class _KoreanValidation(Middleware):
    """필수·nullable JSON schema는 유지하고 입력 검증 오류만 한국어로 바꾼다."""

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        try:
            return await call_next(context)
        except (FastMCPValidationError, PydanticValidationError) as exc:
            raise ToolError("도구 입력 형식이 올바르지 않습니다. 필수 문자열 인자와 명시적인 null 허용 여부를 확인하세요.") from exc


def create_server(registry: Registry | None = None) -> FastMCP:
    """테스트에서는 Registry를 주입하고 운영에서는 환경변수로 지연 생성한다."""
    server = FastMCP(
        "KB Registry",
        middleware=[_KoreanValidation()],
        instructions=(
            "문서 파일을 절대 수정하지 않는 식별자 Registry입니다. "
            "allocate_document의 expected_frontmatter를 사용자가 파일에 적용한 뒤 confirm_document를 호출하세요. "
            "id는 임의 생성하지 마세요. reconcile_document는 읽기 전용 제안입니다."
        ),
    )

    def invoke(method: str, **kwargs: Any) -> Any:
        nonlocal registry
        try:
            if registry is None:
                root = os.environ.get("KB_REGISTRY_ROOT")
                if not root:
                    raise RegistryError("KB_REGISTRY_ROOT에 문서 root 절대 경로를 설정하세요.")
                if not os.path.isabs(root):
                    raise RegistryError("KB_REGISTRY_ROOT는 절대 경로여야 합니다.")
                registry = Registry(root)
            return getattr(registry, method)(**kwargs)
        except RegistryError as exc:
            raise ToolError(str(exc)) from exc
        except (TypeError, ValueError, OSError) as exc:
            logging.getLogger(__name__).warning("도구 입력 또는 파일 접근 오류: %s", exc)
            raise ToolError("입력 형식 또는 파일 접근 권한을 확인하세요.") from exc

    def text(value: Any, field: str, optional: bool = False) -> str | None:
        if optional and value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ToolError(f"{field}는 비어 있지 않은 문자열이어야 합니다.")
        return value

    @server.tool()
    def allocate_document(path: str, category: str | None = None, idempotency_key: str | None = None) -> dict:
        """Pending 식별자를 예약한다. path는 root 내부 .md/.markdown/.qmd 상대 경로 문자열(신규 경로 가능), category는 RL 같은 대문자·숫자 code 문자열 또는 null, idempotency_key는 문자열 또는 null이다."""
        return invoke("allocate_document", path=text(path, "path"), category=text(category, "category", True), idempotency_key=text(idempotency_key, "idempotency_key", True))

    @server.tool()
    def confirm_document(kb_id: str) -> dict:
        """실제 파일 YAML이 예약 metadata와 일치할 때만 Committed로 확정한다."""
        return invoke("confirm_document", kb_id=text(kb_id, "kb_id"))

    @server.tool()
    def get_document(kb_id: str) -> dict:
        """문서 상태와 canonical expected_frontmatter를 조회한다."""
        return invoke("get_document", kb_id=text(kb_id, "kb_id"))

    @server.tool()
    def assign_category(kb_id: str, category: str | None) -> dict:
        """kb_id(KB-000001 문자열)에 category(RL 같은 대문자·숫자 code 문자열 또는 null)를 배정한다. 파일은 수정하지 않는다."""
        return invoke("assign_category", kb_id=text(kb_id, "kb_id"), category=text(category, "category", True))

    @server.tool()
    def remove_category(kb_id: str) -> dict:
        """문서 category를 null로 바꾸고 과거 번호 예약은 보존한다."""
        return invoke("remove_category", kb_id=text(kb_id, "kb_id"))

    @server.tool()
    def link_youtrack(kb_id: str, issue_id: str) -> dict:
        """YouTrack issue ID를 연결한다. 네트워크 요청은 하지 않는다."""
        return invoke("link_youtrack", kb_id=text(kb_id, "kb_id"), issue_id=text(issue_id, "issue_id"))

    @server.tool()
    def update_document_path(kb_id: str, path: str) -> dict:
        """kb_id 문자열의 이동을 확인한다. path는 root 내부 .md/.markdown/.qmd 상대 경로 문자열이며 파일에 실제 id가 있어야 한다."""
        return invoke("update_document_path", kb_id=text(kb_id, "kb_id"), path=text(path, "path"))

    @server.tool()
    def reconcile_document(kb_id: str, path: str | None = None) -> dict:
        """DB·파일을 수정하지 않고 복구 제안만 반환한다. kb_id는 KB-000001 문자열, path는 root 내부 문서 상대 경로 문자열 또는 null(등록 경로)이다."""
        return invoke("reconcile_document", kb_id=text(kb_id, "kb_id"), path=text(path, "path", True))

    @server.tool()
    def create_category(code: str, name: str | None = None) -> dict:
        """안정 internal id를 가진 category를 생성한다."""
        return invoke("create_category", code=text(code, "code"), name=text(name, "name", True))

    @server.tool()
    def list_categories() -> list[dict]:
        """삭제 상태 및 과거 code alias를 포함한 category 목록을 조회한다."""
        return invoke("list_categories")

    @server.tool()
    def rename_category(code: str, new_code: str, name: str | None = None) -> dict:
        """code를 변경하되 internal id·sequence·과거 display ID 예약을 보존한다."""
        return invoke("rename_category", code=text(code, "code"), new_code=text(new_code, "new_code"), name=text(name, "name", True))

    @server.tool()
    def delete_category(code: str) -> dict:
        """category를 soft delete하고 연결 문서 category를 null로 바꾼다."""
        return invoke("delete_category", code=text(code, "code"))

    @server.tool()
    def validate_registry() -> dict:
        """파일 누락·미등록·복제 id·YAML 불일치·Pending 및 SQLite 무결성을 검사한다."""
        return invoke("validate_registry")

    return server


mcp = create_server()


def main() -> None:
    """stdout에는 MCP 메시지만 내보내고 운영 로그는 stderr로 보낸다."""
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    # 라이브러리의 영문 시작/요청 로그는 억제하고 한국어 도구 오류는 응답으로 전달한다.
    mcp.run(transport="stdio", show_banner=False, log_level="CRITICAL")


if __name__ == "__main__":
    main()
