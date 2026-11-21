"""SQLite 트랜잭션으로 식별자를 예약하고 문서는 읽기만 하는 비즈니스 API."""

from __future__ import annotations

import json
import fcntl
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import yaml


class RegistryError(ValueError):
    """입력, 파일 상태 또는 Registry 불변식 위반."""


class _UniqueLoader(yaml.SafeLoader):
    """중복 mapping key를 허용하지 않는 안전한 YAML loader."""


def _mapping(loader: _UniqueLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            if key in result:
                raise RegistryError(f"YAML 중복 key: {key}")
            result[key] = loader.construct_object(value_node, deep=deep)
        except TypeError as exc:
            raise RegistryError("YAML key는 hash 가능한 값이어야 합니다.") from exc
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
_KB = re.compile(r"KB-([0-9]{6,})\Z")
_CODE = re.compile(r"[A-Z][A-Z0-9]{0,15}\Z")
_DISPLAY = re.compile(r"([A-Z][A-Z0-9]{0,15})-([0-9]{3,})\Z")
_ISSUE = re.compile(r"[A-Z][A-Z0-9_]*-[1-9][0-9]*\Z")
_KEYS = ("id", "category", "category_number", "youtrack_issues")


class Registry:
    """root에 묶인 Registry. 각 호출마다 별도 연결과 BEGIN IMMEDIATE를 사용한다."""

    def __init__(self, root: str | Path | None = None, db_path: str | Path | None = None, *, kb_root: str | Path | None = None):
        if root is None:
            root = kb_root or os.environ.get("KB_REGISTRY_ROOT")
        elif kb_root is not None and Path(root).resolve() != Path(kb_root).resolve():
            raise RegistryError("root와 kb_root가 서로 다릅니다.")
        if root is None:
            raise RegistryError("문서 root를 지정하거나 KB_REGISTRY_ROOT를 설정하세요.")
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise RegistryError("문서 root가 존재하는 디렉터리가 아닙니다.")
        default_db = Path(__file__).resolve().parents[2] / "data" / "registry.sqlite3"
        self.db_path = Path(db_path or os.environ.get("KB_REGISTRY_DB", default_db)).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # 최초 WAL 전환은 busy_timeout만으로 동시 초기화를 보장하지 못한다.
            # 잠금 파일은 지우지 않아 대기 중인 프로세스가 다른 inode를 잠그지 않게 한다.
            with self.db_path.with_name(self.db_path.name + ".init.lock").open("a+b") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    with self._connection() as db:
                        db.execute("PRAGMA journal_mode=WAL")
                        version = db.execute("PRAGMA user_version").fetchone()[0]
                        if version not in (0, 1):
                            raise RegistryError("지원하지 않는 Registry DB schema 버전입니다.")
                        db.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
                    with self._transaction() as db:
                        row = db.execute("SELECT value FROM settings WHERE key='root'").fetchone()
                        if row and row[0] != str(self.root):
                            raise RegistryError("이 DB는 다른 문서 root에 연결되어 있습니다.")
                        db.execute("INSERT OR IGNORE INTO settings VALUES ('root', ?)", (str(self.root),))
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
        except OSError as exc:
            raise RegistryError(f"Registry DB 초기화에 실패했습니다: {exc}") from exc

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = None
        try:
            db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("PRAGMA foreign_keys=ON")
            yield db
        except sqlite3.Error as exc:
            raise RegistryError(f"Registry DB 작업에 실패했습니다: {exc}") from exc
        finally:
            if db is not None:
                db.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @contextmanager
    def _readonly(self) -> Iterator[sqlite3.Connection]:
        """여러 SELECT가 같은 WAL snapshot을 보도록 읽기 트랜잭션을 연다."""
        with self._connection() as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            try:
                yield db
            finally:
                db.rollback()

    def _path(self, path: str | Path, *, require_exists: bool = True) -> tuple[Path, str]:
        try:
            raw = Path(path)
            if not str(path) or "\x00" in str(path) or "\\" in str(path) or ".." in raw.parts:
                raise RegistryError("안전하지 않은 문서 경로입니다.")
            target = (raw if raw.is_absolute() else self.root / raw).resolve()
            if not target.is_relative_to(self.root):
                raise RegistryError("문서 root 밖의 경로 또는 symlink는 허용하지 않습니다.")
            if target.suffix.lower() not in {".md", ".markdown", ".qmd"}:
                raise RegistryError(".md, .markdown, .qmd 문서만 허용합니다.")
            if require_exists and not target.is_file():
                raise RegistryError("문서 파일이 존재하지 않습니다.")
            if target.exists() and not target.is_file():
                raise RegistryError("문서 경로가 일반 파일이 아닙니다.")
            return target, target.relative_to(self.root).as_posix()
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            if isinstance(exc, RegistryError):
                raise
            raise RegistryError(f"문서 경로를 확인할 수 없습니다: {exc}") from exc

    @staticmethod
    def _kb_number(kb_id: str) -> int:
        match = _KB.fullmatch(kb_id) if isinstance(kb_id, str) else None
        if not match or int(match[1]) < 1 or kb_id != f"KB-{int(match[1]):06d}":
            raise RegistryError("id는 KB-000001 형식의 양수여야 합니다.")
        return int(match[1])

    @staticmethod
    def _code(code: str) -> str:
        if not isinstance(code, str) or not _CODE.fullmatch(code):
            raise RegistryError("category code는 영문 대문자로 시작하는 대문자·숫자 1~16자여야 합니다.")
        return code

    @staticmethod
    def _display(value: str) -> tuple[str, int]:
        match = _DISPLAY.fullmatch(value) if isinstance(value, str) else None
        if not match or int(match[2]) < 1 or value != f"{match[1]}-{int(match[2]):03d}":
            raise RegistryError("category_number는 RL-001 형식의 양수 문자열이어야 합니다.")
        return match[1], int(match[2])

    @staticmethod
    def _issue(issue_id: str) -> str:
        if not isinstance(issue_id, str) or not _ISSUE.fullmatch(issue_id):
            raise RegistryError("YouTrack issue ID 형식이 올바르지 않습니다.")
        return issue_id

    def _read(self, path: str | Path) -> dict:
        target, _ = self._path(path)
        try:
            text = target.read_text(encoding="utf-8-sig")
            lines = text.splitlines()
            if not lines or lines[0].strip() != "---":
                return {}
            end = next((i for i in range(1, len(lines)) if lines[i].strip() in {"---", "..."}), None)
            if end is None:
                raise RegistryError("YAML frontmatter 종료 구분자가 없습니다.")
            meta = yaml.load("\n".join(lines[1:end]), Loader=_UniqueLoader)
            if meta is None:
                meta = {}
            if not isinstance(meta, dict) or any(not isinstance(k, str) for k in meta):
                raise RegistryError("YAML frontmatter는 문자열 key의 mapping이어야 합니다.")
            if "id" in meta and meta["id"] is not None:
                self._kb_number(meta["id"])
            if meta.get("category") is not None:
                self._code(meta["category"])
            if meta.get("category_number") is not None:
                self._display(meta["category_number"])
                if meta.get("category") is None or meta.get("id") is None:
                    raise RegistryError("category_number에는 id와 category가 필요합니다.")
            if "youtrack_issues" in meta:
                issues = meta["youtrack_issues"]
                if not isinstance(issues, list) or any(not isinstance(v, str) for v in issues):
                    raise RegistryError("youtrack_issues는 문자열 list여야 합니다.")
                for issue in issues:
                    self._issue(issue)
                if len(set(issues)) != len(issues):
                    raise RegistryError("youtrack_issues에 중복 issue가 있습니다.")
            return meta
        except (OSError, UnicodeError, yaml.YAMLError, RecursionError) as exc:
            raise RegistryError(f"문서 YAML을 읽을 수 없습니다: {exc}") from exc

    def _scan(self) -> tuple[dict[str, dict], list[dict]]:
        files, issues = {}, []
        def walk_error(exc: OSError) -> None:
            issues.append({"type": "unsafe_path", "path": str(exc.filename), "message": "디렉터리를 읽을 수 없습니다."})
        for directory, dirs, names in os.walk(self.root, followlinks=False, onerror=walk_error):
            for name in list(dirs):
                child = Path(directory) / name
                if child.is_symlink():
                    dirs.remove(name)
                    issues.append({"type": "unsafe_path", "path": str(child), "message": "symlink 디렉터리는 스캔하지 않습니다."})
            for name in sorted(names):
                child = Path(directory) / name
                if child.suffix.lower() not in {".md", ".markdown", ".qmd"}:
                    continue
                relative = child.relative_to(self.root).as_posix()
                try:
                    _, canonical = self._path(child)
                    if canonical in files:
                        continue
                    files[canonical] = self._read(child)
                except RegistryError as exc:
                    issues.append({"type": "malformed", "path": relative, "message": str(exc)})
        seen = {}
        for path, meta in files.items():
            kb_id = meta.get("id")
            if kb_id:
                if kb_id in seen:
                    issues.append({"type": "duplicate", "kb_id": kb_id, "paths": [seen[kb_id], path]})
                seen[kb_id] = path
        return files, issues

    def _safe_scan(self) -> dict[str, dict]:
        files, issues = self._scan()
        if issues:
            raise RegistryError("전체 문서 스캔에 실패했습니다: " + json.dumps(issues, ensure_ascii=False))
        return files

    @staticmethod
    def _history(db: sqlite3.Connection, kb_id: str | None, event: str, details: dict) -> None:
        db.execute("INSERT INTO history(kb_id,event,details) VALUES (?,?,?)", (kb_id, event, json.dumps(details, ensure_ascii=False)))

    def _category(self, db: sqlite3.Connection, code: str, *, create: bool = False, active: bool = True) -> sqlite3.Row:
        self._code(code)
        row = db.execute("SELECT c.* FROM categories c JOIN category_aliases a ON a.category_id=c.id WHERE a.code=?", (code,)).fetchone()
        if row is None and create:
            cur = db.execute("INSERT INTO categories(code) VALUES (?)", (code,))
            db.execute("INSERT INTO category_aliases VALUES (?,?)", (code, cur.lastrowid))
            self._history(db, None, "category_created", {"code": code, "category_id": cur.lastrowid})
            row = db.execute("SELECT * FROM categories WHERE id=?", (cur.lastrowid,)).fetchone()
        if row is None:
            raise RegistryError(f"등록되지 않은 category입니다: {code}")
        if active and not row["active"]:
            raise RegistryError(f"삭제된 category code는 다시 사용할 수 없습니다: {code}")
        return row

    def _reserve_display(self, db: sqlite3.Connection, value: str, category_id: int, kb_id: str) -> None:
        code, number = self._display(value)
        category = self._category(db, code, active=False)
        if category["id"] != category_id:
            raise RegistryError("category와 category_number의 code가 다릅니다.")
        rows = db.execute("SELECT * FROM category_reservations WHERE display_id=? OR (category_id=? AND number=?)", (value, category_id, number)).fetchall()
        if any(row["kb_id"] != kb_id or row["category_id"] != category_id for row in rows):
            raise RegistryError(f"이미 다른 문서가 예약한 category_number입니다: {value}")
        db.execute("INSERT OR IGNORE INTO category_reservations VALUES (?,?,?,?)", (value, category_id, number, kb_id))
        db.execute("UPDATE categories SET next_number=MAX(next_number,?) WHERE id=?", (number + 1, category_id))

    def _reserve_scan(self, db: sqlite3.Connection, files: dict[str, dict]) -> None:
        for path, meta in files.items():
            kb_id = meta.get("id")
            owner = db.execute("SELECT kb_id FROM documents WHERE path=?", (path,)).fetchone()
            if owner:
                if kb_id not in (None, owner["kb_id"]):
                    raise RegistryError(f"등록된 경로의 파일 id가 충돌합니다: {path}")
                # 등록 이후에는 DB가 SoT다. 파일의 변조·구버전 metadata를 재수입하지 않는다.
                continue
            if not kb_id:
                continue
            registered = db.execute("SELECT path FROM documents WHERE kb_id=?", (kb_id,)).fetchone()
            if registered and registered["path"] != path:
                raise RegistryError(f"등록 경로와 파일 id가 충돌합니다. update_document_path를 사용하세요: {kb_id}")
            db.execute("INSERT OR IGNORE INTO id_reservations VALUES (?,?)", (kb_id, self._kb_number(kb_id)))
            value = meta.get("category_number")
            if value:
                cat = self._category(db, meta["category"], create=True, active=False)
                code, _ = self._display(value)
                self._category(db, code, create=(code == meta["category"]), active=False)
                self._reserve_display(db, value, cat["id"], kb_id)

    def _document(self, db: sqlite3.Connection, kb_id: str) -> dict:
        self._kb_number(kb_id)
        row = db.execute("SELECT d.*, c.code AS category FROM documents d LEFT JOIN categories c ON c.id=d.category_id WHERE kb_id=?", (kb_id,)).fetchone()
        if row is None:
            raise RegistryError(f"등록되지 않은 문서입니다: {kb_id}")
        doc = dict(row)
        doc["youtrack_issues"] = [r[0] for r in db.execute("SELECT issue_id FROM youtrack_links WHERE kb_id=? ORDER BY issue_id", (kb_id,))]
        doc["expected_frontmatter"] = {"id": kb_id, "category": doc["category"], "category_number": doc["category_number"], "youtrack_issues": doc["youtrack_issues"]}
        return doc

    @staticmethod
    def _pending(db: sqlite3.Connection, kb_id: str) -> None:
        db.execute("UPDATE documents SET status='pending',updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE kb_id=?", (kb_id,))

    def _assign(self, db: sqlite3.Connection, kb_id: str, category: str | None) -> None:
        doc = self._document(db, kb_id)
        cat = self._category(db, category, create=True) if category is not None else None
        category_id = cat["id"] if cat else None
        if doc["category_id"] == category_id:
            return
        display = None
        if cat:
            display = f"{cat['code']}-{cat['next_number']:03d}"
            self._reserve_display(db, display, category_id, kb_id)
        db.execute("UPDATE documents SET category_id=?,category_number=? WHERE kb_id=?", (category_id, display, kb_id))
        self._pending(db, kb_id)
        self._history(db, kb_id, "category_assigned", {"previous": doc["category_number"], "category_number": display})

    def allocate_document(self, path: str | Path, category: str | None = None, idempotency_key: str | None = None) -> dict:
        """파일에 실제 존재하는 id만 import하며 나머지는 전체 스캔 후 신규 예약한다."""
        _, relative = self._path(path, require_exists=False)
        if category is not None:
            self._code(category)
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key.strip()):
            raise RegistryError("idempotency_key는 비어 있지 않은 문자열이어야 합니다.")
        with self._transaction() as db:
            files = self._safe_scan()
            self._reserve_scan(db, files)
            meta = files.get(relative, {})
            if idempotency_key is not None:
                prior = db.execute("SELECT * FROM idempotency_keys WHERE key=?", (idempotency_key,)).fetchone()
                if prior:
                    if prior["path"] != relative:
                        raise RegistryError("idempotency_key를 다른 경로에 재사용할 수 없습니다.")
                    existing = self._document(db, prior["kb_id"])
                    if existing["path"] != relative:
                        raise RegistryError("이 예약의 문서는 이동되었습니다. 기존 id로 조회하세요.")
                    if meta.get("id") not in (None, existing["kb_id"]):
                        raise RegistryError("파일 id가 기존 예약과 충돌합니다.")
                    return existing
            existing = db.execute("SELECT kb_id FROM documents WHERE path=?", (relative,)).fetchone()
            if existing:
                kb_id = existing[0]
                if meta.get("id") not in (None, kb_id):
                    raise RegistryError("파일 id가 기존 예약과 충돌합니다.")
            else:
                kb_id = meta.get("id")
                if kb_id is None:
                    number = db.execute("SELECT COALESCE(MAX(number),0)+1 FROM id_reservations").fetchone()[0]
                    kb_id = f"KB-{number:06d}"
                    db.execute("INSERT INTO id_reservations VALUES (?,?)", (kb_id, number))
                db.execute("INSERT INTO documents(kb_id,path) VALUES (?,?)", (kb_id, relative))
                self._history(db, kb_id, "allocated", {"path": relative, "imported": meta.get("id") is not None})
                selected = category if category is not None else meta.get("category")
                if selected is not None:
                    cat = self._category(db, selected, create=True)
                    if meta.get("category_number") and meta.get("category") == selected:
                        _, number = self._display(meta["category_number"])
                        display = f"{cat['code']}-{number:03d}"
                        self._reserve_display(db, display, cat["id"], kb_id)
                        db.execute("UPDATE documents SET category_id=?,category_number=? WHERE kb_id=?", (cat["id"], display, kb_id))
                    else:
                        self._assign(db, kb_id, selected)
                for issue in meta.get("youtrack_issues", []):
                    db.execute("INSERT INTO youtrack_links VALUES (?,?)", (kb_id, issue))
            if idempotency_key is not None:
                db.execute("INSERT INTO idempotency_keys VALUES (?,?,?)", (idempotency_key, kb_id, relative))
            return self._document(db, kb_id)

    def get_document(self, kb_id: str) -> dict:
        """현재 DB 상태와 Agent가 직접 적용해야 할 canonical frontmatter를 반환한다."""
        with self._readonly() as db:
            return self._document(db, kb_id)

    @staticmethod
    def _matches(meta: dict, expected: dict) -> bool:
        if any(key not in meta for key in _KEYS):
            return False
        actual = {key: meta[key] for key in _KEYS}
        actual["youtrack_issues"] = sorted(actual["youtrack_issues"])
        return actual == expected

    def confirm_document(self, kb_id: str) -> dict:
        """전체 스캔으로 복제 id를 배제하고 실제 YAML 일치 시에만 확정한다."""
        mismatch = None
        with self._transaction() as db:
            doc = self._document(db, kb_id)
            try:
                files = self._safe_scan()
                meta = files.get(doc["path"])
                if meta is None or not self._matches(meta, doc["expected_frontmatter"]):
                    mismatch = "파일 frontmatter가 expected_frontmatter와 일치하지 않습니다."
                else:
                    conflicts = self._file_number_issues(db, files) + self._db_invariant_issues(db)
                    if conflicts:
                        mismatch = "번호 충돌 또는 Registry 무결성 오류로 확정할 수 없습니다: " + json.dumps(conflicts, ensure_ascii=False)
            except RegistryError as exc:
                mismatch = str(exc)
            if mismatch:
                self._pending(db, kb_id)
                self._history(db, kb_id, "confirmation_failed", {"message": mismatch})
            else:
                db.execute("UPDATE documents SET status='committed',updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE kb_id=?", (kb_id,))
                self._history(db, kb_id, "confirmed", {})
            result = self._document(db, kb_id)
        if mismatch:
            raise RegistryError(mismatch)
        return result

    def assign_category(self, kb_id: str, category: str | None) -> dict:
        """문서 category를 하나 또는 null로 바꾸고 이전 번호 예약은 보존한다."""
        with self._transaction() as db:
            self._reserve_scan(db, self._safe_scan())
            self._assign(db, kb_id, category)
            return self._document(db, kb_id)

    def remove_category(self, kb_id: str) -> dict:
        """문서의 category를 제거한다. 이전 display ID는 재사용하지 않는다."""
        return self.assign_category(kb_id, None)

    def link_youtrack(self, kb_id: str, issue_id: str) -> dict:
        """네트워크 호출 없이 YouTrack issue ID를 저장한다."""
        self._issue(issue_id)
        with self._transaction() as db:
            self._document(db, kb_id)
            cur = db.execute("INSERT OR IGNORE INTO youtrack_links VALUES (?,?)", (kb_id, issue_id))
            if cur.rowcount:
                self._pending(db, kb_id)
                self._history(db, kb_id, "youtrack_linked", {"issue_id": issue_id})
            return self._document(db, kb_id)

    def update_document_path(self, kb_id: str, path: str | Path) -> dict:
        """실제 id를 가진 이동 대상만 허용하며 이전 경로와 history를 보존한다."""
        _, relative = self._path(path)
        with self._transaction() as db:
            doc = self._document(db, kb_id)
            files = self._safe_scan()
            if files[relative].get("id") != kb_id:
                raise RegistryError("이동 대상 파일에 문서의 실제 id가 있어야 합니다.")
            if doc["path"] != relative:
                if doc["path"] in files:
                    raise RegistryError("이전 경로에 파일이 남아 있습니다. 이동인지 먼저 확인하세요.")
                db.execute("UPDATE documents SET path=? WHERE kb_id=?", (relative, kb_id))
                self._pending(db, kb_id)
                self._history(db, kb_id, "path_updated", {"previous": doc["path"], "path": relative})
            return self._document(db, kb_id)

    def _category_dict(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict:
        result = dict(row)
        result["active"] = bool(result["active"])
        result["aliases"] = [r[0] for r in db.execute("SELECT code FROM category_aliases WHERE category_id=? ORDER BY code", (row["id"],))]
        return result

    def create_category(self, code: str, name: str | None = None) -> dict:
        """새 category를 만들거나 기존 active category를 반환한다."""
        with self._transaction() as db:
            row = self._category(db, code, create=True)
            if name is not None:
                if not isinstance(name, str):
                    raise RegistryError("category name은 문자열 또는 null이어야 합니다.")
                db.execute("UPDATE categories SET name=? WHERE id=?", (name, row["id"]))
            return self._category_dict(db, db.execute("SELECT * FROM categories WHERE id=?", (row["id"],)).fetchone())

    def list_categories(self) -> list[dict]:
        """삭제된 category와 영구 예약 alias를 포함해 반환한다."""
        with self._readonly() as db:
            return [self._category_dict(db, row) for row in db.execute("SELECT * FROM categories ORDER BY id")]

    def rename_category(self, code: str, new_code: str, name: str | None = None) -> dict:
        """internal id와 sequence를 유지하고 이전 code 및 display ID를 예약한다."""
        self._code(new_code)
        if name is not None and not isinstance(name, str):
            raise RegistryError("category name은 문자열 또는 null이어야 합니다.")
        with self._transaction() as db:
            self._reserve_scan(db, self._safe_scan())
            cat = self._category(db, code)
            prior = db.execute("SELECT category_id FROM category_aliases WHERE code=?", (new_code,)).fetchone()
            if prior and prior[0] != cat["id"]:
                raise RegistryError("이미 다른 category가 예약한 code입니다.")
            if new_code != cat["code"]:
                db.execute("INSERT OR IGNORE INTO category_aliases VALUES (?,?)", (new_code, cat["id"]))
                db.execute("UPDATE categories SET code=? WHERE id=?", (new_code, cat["id"]))
                reservations = db.execute("SELECT DISTINCT number,kb_id FROM category_reservations WHERE category_id=?", (cat["id"],)).fetchall()
                for row in reservations:
                    self._reserve_display(db, f"{new_code}-{row['number']:03d}", cat["id"], row["kb_id"])
                docs = db.execute("SELECT kb_id,category_number FROM documents WHERE category_id=?", (cat["id"],)).fetchall()
                for doc in docs:
                    _, number = self._display(doc["category_number"])
                    db.execute("UPDATE documents SET category_number=? WHERE kb_id=?", (f"{new_code}-{number:03d}", doc["kb_id"]))
                    self._pending(db, doc["kb_id"])
                    self._history(db, doc["kb_id"], "category_renamed", {"previous": cat["code"], "code": new_code})
                self._history(db, None, "category_renamed", {"category_id": cat["id"], "previous": cat["code"], "code": new_code})
            if name is not None:
                db.execute("UPDATE categories SET name=? WHERE id=?", (name, cat["id"]))
            return self._category_dict(db, db.execute("SELECT * FROM categories WHERE id=?", (cat["id"],)).fetchone())

    def delete_category(self, code: str) -> dict:
        """soft delete하고 관련 문서는 null category와 Pending으로 전환한다."""
        with self._transaction() as db:
            cat = self._category(db, code, active=False)
            if cat["active"]:
                for row in db.execute("SELECT kb_id FROM documents WHERE category_id=?", (cat["id"],)).fetchall():
                    self._assign(db, row["kb_id"], None)
                db.execute("UPDATE categories SET active=0 WHERE id=?", (cat["id"],))
                self._history(db, None, "category_deleted", {"category_id": cat["id"], "code": cat["code"]})
            return self._category_dict(db, db.execute("SELECT * FROM categories WHERE id=?", (cat["id"],)).fetchone())

    def reconcile_document(self, kb_id: str, path: str | Path | None = None) -> dict:
        """파일·DB를 수정하지 않고 필요한 YAML 또는 경로 변경 proposal만 반환한다."""
        with self._readonly() as db:
            doc = self._document(db, kb_id)
            relative = self._path(path)[1] if path is not None else doc["path"]
            files, issues = self._scan()
            meta = files.get(relative)
            if meta is None:
                issues.append({"type": "missing", "path": relative})
            elif path is not None and meta.get("id") != kb_id:
                issues.append({"type": "id_conflict", "path": relative, "message": "이동 대상에 실제 id가 없습니다."})
            elif not self._matches(meta, doc["expected_frontmatter"]):
                issues.append({"type": "frontmatter_mismatch", "path": relative, "kb_id": kb_id})
            if relative != doc["path"]:
                issues.append({"type": "path_mismatch", "path": relative, "registered_path": doc["path"]})
            return {"document": doc, "matches": not issues, "issues": issues, "proposal": {"path": relative, "expected_frontmatter": doc["expected_frontmatter"]}, "read_only": True}

    def _file_number_issues(self, db: sqlite3.Connection, files: dict[str, dict]) -> list[dict]:
        """파일끼리와 영구 예약 간 display ID 충돌을 DB 변경 없이 검사한다."""
        issues, seen = [], {}
        for path, meta in files.items():
            value = meta.get("category_number")
            if not value:
                continue
            code, number = self._display(value)
            cat = db.execute("SELECT category_id FROM category_aliases WHERE code=?", (meta["category"],)).fetchone()
            prefix = db.execute("SELECT category_id FROM category_aliases WHERE code=?", (code,)).fetchone()
            if code != meta["category"] and (not cat or not prefix or cat[0] != prefix[0]):
                issues.append({"type": "malformed", "path": path, "message": "category와 category_number의 code가 다릅니다."})
                continue
            logical = (cat[0] if cat else code, number)
            prior = seen.get(logical)
            if prior and prior[1] != meta["id"]:
                issues.append({"type": "category_number_conflict", "paths": [prior[0], path], "category_number": value})
            seen[logical] = (path, meta["id"])
            if cat:
                reserved = db.execute("SELECT kb_id FROM category_reservations WHERE category_id=? AND number=?", (cat[0], number)).fetchall()
                if any(row[0] != meta["id"] for row in reserved):
                    issues.append({"type": "category_number_conflict", "path": path, "category_number": value})
        return issues

    def _db_invariant_issues(self, db: sqlite3.Connection) -> list[dict]:
        """SQL FK만으로 표현하지 못하는 소유권·alias·sequence 불변식을 검사한다."""
        issues = []

        def report(check: str, **details: Any) -> None:
            issues.append({"type": "registry_invariant", "check": check, **details})

        categories = {r["id"]: dict(r) for r in db.execute("SELECT * FROM categories")}
        aliases = {r["code"]: r["category_id"] for r in db.execute("SELECT * FROM category_aliases")}
        ids = {r["kb_id"]: r["number"] for r in db.execute("SELECT * FROM id_reservations")}
        reservations = {r["display_id"]: dict(r) for r in db.execute("SELECT * FROM category_reservations")}
        root = db.execute("SELECT value FROM settings WHERE key='root'").fetchone()
        if root is None or root[0] != str(self.root):
            report("root_binding")
        for kb_id, number in ids.items():
            try:
                if self._kb_number(kb_id) != number:
                    report("id_reservation_number", kb_id=kb_id, number=number)
            except RegistryError:
                report("id_reservation_format", kb_id=kb_id)
        for code, category_id in aliases.items():
            if category_id not in categories:
                report("alias_category_missing", code=code, category_id=category_id)
            try:
                self._code(code)
            except RegistryError:
                report("alias_code_format", code=code)
        for category_id, category in categories.items():
            if aliases.get(category["code"]) != category_id:
                report("category_current_alias", category_id=category_id, code=category["code"])
            if not isinstance(category["next_number"], int) or category["next_number"] < 1:
                report("category_counter_format", category_id=category_id)
            if category["active"] not in (0, 1):
                report("category_active_format", category_id=category_id)
        owners, maximum = {}, {}
        for display, reservation in reservations.items():
            category_id, number, kb_id = (reservation[k] for k in ("category_id", "number", "kb_id"))
            try:
                code, parsed_number = self._display(display)
                if parsed_number != number:
                    report("display_reservation_number", display_id=display)
                if aliases.get(code) != category_id:
                    report("display_alias_owner", display_id=display, category_id=category_id)
            except RegistryError:
                report("display_reservation_format", display_id=display)
            if category_id not in categories:
                report("display_category_missing", display_id=display)
            if kb_id not in ids:
                report("display_id_owner_missing", display_id=display, kb_id=kb_id)
            logical = (category_id, number)
            if logical in owners and owners[logical] != kb_id:
                report("display_logical_owner", display_id=display, kb_id=kb_id)
            owners[logical] = kb_id
            if isinstance(number, int):
                maximum[category_id] = max(maximum.get(category_id, 0), number)
            else:
                report("display_number_format", display_id=display)
        for category_id, category in categories.items():
            if isinstance(category["next_number"], int) and category["next_number"] <= maximum.get(category_id, 0):
                report("category_counter_behind", category_id=category_id, next_number=category["next_number"], reserved_max=maximum.get(category_id, 0))
        for doc in db.execute("SELECT * FROM documents"):
            kb_id, category_id, display = (doc[k] for k in ("kb_id", "category_id", "category_number"))
            if kb_id not in ids:
                report("document_id_reservation_missing", kb_id=kb_id)
            if category_id is None:
                if display is not None:
                    report("document_null_category_number", kb_id=kb_id)
                continue
            category = categories.get(category_id)
            if not category:
                report("document_category_missing", kb_id=kb_id)
            elif not category["active"]:
                report("document_inactive_category", kb_id=kb_id, category_id=category_id)
            reservation = reservations.get(display)
            if reservation is None:
                report("document_display_reservation_missing", kb_id=kb_id, display_id=display)
            else:
                if reservation["kb_id"] != kb_id or reservation["category_id"] != category_id:
                    report("document_display_owner", kb_id=kb_id, display_id=display)
                if category and isinstance(reservation["number"], int) and display != f"{category['code']}-{reservation['number']:03d}":
                    report("document_current_display_code", kb_id=kb_id, display_id=display)
        return issues

    def validate_registry(self) -> dict:
        """파일과 SQLite의 누락·미등록·중복·YAML·Pending·무결성을 읽기 전용 검사한다."""
        with self._readonly() as db:
            files, issues = self._scan()
            for row in db.execute("PRAGMA integrity_check"):
                if row[0] != "ok":
                    issues.append({"type": "sqlite_integrity", "message": row[0]})
            for row in db.execute("PRAGMA foreign_key_check"):
                issues.append({"type": "sqlite_foreign_key", "details": list(row)})
            issues.extend(self._db_invariant_issues(db))
            rows = db.execute("SELECT kb_id FROM documents ORDER BY kb_id").fetchall()
            docs = []
            for row in rows:
                try:
                    docs.append(self._document(db, row[0]))
                except RegistryError as exc:
                    issues.append({"type": "registry_invariant", "check": "document_format", "kb_id": row[0], "message": str(exc)})
            by_path = {doc["path"]: doc for doc in docs}
            by_id = {doc["kb_id"]: doc for doc in docs}
            for doc in docs:
                meta = files.get(doc["path"])
                if meta is None:
                    issues.append({"type": "missing", "kb_id": doc["kb_id"], "path": doc["path"]})
                elif not self._matches(meta, doc["expected_frontmatter"]):
                    issues.append({"type": "frontmatter_mismatch", "kb_id": doc["kb_id"], "path": doc["path"]})
                if doc["status"] == "pending":
                    issues.append({"type": "pending", "kb_id": doc["kb_id"], "path": doc["path"]})
            for path, meta in files.items():
                if path not in by_path:
                    issues.append({"type": "unregistered", "path": path, "kb_id": meta.get("id")})
                registered = by_id.get(meta.get("id"))
                if registered and registered["path"] != path:
                    issues.append({"type": "id_conflict", "path": path, "kb_id": meta["id"], "registered_path": registered["path"]})
            issues.extend(self._file_number_issues(db, files))
            return {"ok": not issues, "issues": issues, "document_count": len(rows), "file_count": len(files)}
