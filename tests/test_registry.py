"""실제 KB와 분리된 임시 파일·DB 기반 회귀 검증."""

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import sqlite3

import pytest
import yaml

from kb_registry.registry import Registry, RegistryError


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "Documents"
    root.mkdir()
    db = tmp_path / "registry.sqlite3"
    registry = Registry(db_path=db, kb_root=root)
    return registry, root, db


def write_document(root, document, path=None):
    target = root / (path or document["path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\n" + yaml.safe_dump(document["expected_frontmatter"], allow_unicode=True)
        + "---\n# 테스트 문서\n\n수식: $x^2$\n", encoding="utf-8"
    )
    return target


def allocate_process(args):
    db, root, number = args
    return Registry(db_path=db, kb_root=root).allocate_document(f"{number}.md", category="RL")


def allocate_same_process(args):
    db, root = args
    return Registry(db_path=db, kb_root=root).allocate_document("same.md", category="RL", idempotency_key="same")


def test_basic_pending_confirm_and_retry(env):
    r, root, _ = env
    d = r.allocate_document("any/depth/test.md", category="RL", idempotency_key="request-1")
    assert d["kb_id"] == "KB-000001"
    assert d["expected_frontmatter"]["category_number"] == "RL-001"
    assert r.allocate_document("any/depth/test.md", category="RL", idempotency_key="request-1")["kb_id"] == d["kb_id"]
    with pytest.raises(RegistryError):
        r.confirm_document(d["kb_id"])
    write_document(root, d)
    committed = r.confirm_document(d["kb_id"])
    assert committed["status"] == "committed"
    assert r.confirm_document(d["kb_id"])["status"] == "committed"
    assert r.get_document(d["kb_id"])["path"] == "any/depth/test.md"


def test_thread_concurrency_unique_ids_and_numbers(env):
    r, _, _ = env
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda n: r.allocate_document(f"thread-{n}.md", category="RL"), range(32)))
    assert len({d["kb_id"] for d in results}) == 32
    assert len({d["expected_frontmatter"]["category_number"] for d in results}) == 32


def test_multiprocess_concurrency(env):
    _, root, db = env
    with ProcessPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(allocate_process, [(db, root, n) for n in range(12)]))
    assert len({d["kb_id"] for d in results}) == 12
    assert len({d["expected_frontmatter"]["category_number"] for d in results}) == 12


def test_multiprocess_idempotency(env):
    _, root, db = env
    with ProcessPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(allocate_same_process, [(db, root)] * 12))
    assert len({d["kb_id"] for d in results}) == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM history WHERE event='allocated'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM history WHERE event='category_assigned'").fetchone()[0] == 1


def test_concurrent_retries_same_path(env):
    r, _, _ = env
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: r.allocate_document("same.md", category="RL"), range(12)))
    assert len({d["kb_id"] for d in results}) == 1
    assert r.allocate_document("next.md", category="RL")["expected_frontmatter"]["category_number"] == "RL-002"


def test_category_change_null_reassign_and_no_reuse(env):
    r, _, _ = env
    d = r.allocate_document("a.md", category="RL")
    kb_id = d["kb_id"]
    assert r.assign_category(kb_id, "RL")["expected_frontmatter"]["category_number"] == "RL-001"
    assert r.assign_category(kb_id, "ML")["expected_frontmatter"]["category_number"] == "ML-001"
    assert r.remove_category(kb_id)["expected_frontmatter"]["category"] is None
    assert r.remove_category(kb_id)["expected_frontmatter"]["category_number"] is None
    assert r.assign_category(kb_id, "RL")["expected_frontmatter"]["category_number"] == "RL-002"
    assert r.assign_category(kb_id, None)["expected_frontmatter"]["category"] is None
    assert r.allocate_document("b.md", category="RL")["expected_frontmatter"]["category_number"] == "RL-003"


def test_category_delete_preserves_document(env):
    r, _, _ = env
    d = r.allocate_document("a.md", category="RL")
    r.delete_category("RL")
    assert r.get_document(d["kb_id"])["expected_frontmatter"]["category"] is None
    with pytest.raises(RegistryError):
        r.assign_category(d["kb_id"], "RL")


def test_category_rename_reserves_old_code(env):
    r, _, _ = env
    d = r.allocate_document("a.md", category="RL")
    r.rename_category("RL", "MARL")
    d = r.get_document(d["kb_id"])
    assert d["expected_frontmatter"]["category"] == "MARL"
    assert r.allocate_document("b.md", category="MARL")["expected_frontmatter"]["category_number"] == "MARL-002"
    r.delete_category("MARL")
    with pytest.raises(RegistryError):
        r.allocate_document("c.md", category="RL")


def test_path_move_preserves_identity(env):
    r, root, _ = env
    d = r.allocate_document("old.md", category="RL")
    old = write_document(root, d)
    r.confirm_document(d["kb_id"])
    target = root / "new" / "name.qmd"
    target.parent.mkdir()
    old.rename(target)
    moved = r.update_document_path(d["kb_id"], "new/name.qmd")
    assert moved["kb_id"] == d["kb_id"]
    assert moved["expected_frontmatter"]["category_number"] == "RL-001"
    assert r.confirm_document(d["kb_id"])["status"] == "committed"
    assert r.allocate_document("new/name.qmd")["kb_id"] == d["kb_id"]


def test_idempotency_key_conflict(env):
    r, _, _ = env
    r.allocate_document("a.md", idempotency_key="one")
    with pytest.raises(RegistryError):
        r.allocate_document("b.md", idempotency_key="one")
    assert r.allocate_document("b.md")["kb_id"] == "KB-000002"


def test_partial_failure_metadata_update_retry(env):
    r, root, _ = env
    d = r.allocate_document("a.md", category="RL")
    write_document(root, d)
    r.confirm_document(d["kb_id"])
    changed = r.assign_category(d["kb_id"], "ML")
    assert changed["status"] == "pending"
    with pytest.raises(RegistryError):
        r.confirm_document(d["kb_id"])
    changed_again = r.assign_category(d["kb_id"], "ML")
    assert changed_again["expected_frontmatter"] == changed["expected_frontmatter"]
    write_document(root, changed_again)
    assert r.confirm_document(d["kb_id"])["status"] == "committed"


def test_reconcile_is_read_only(env):
    r, root, _ = env
    d = r.allocate_document("a.md", category="RL")
    p = write_document(root, d)
    r.confirm_document(d["kb_id"])
    p.write_text(p.read_text().replace("RL-001", "RL-999"))
    before = p.read_bytes()
    proposal = r.reconcile_document(d["kb_id"])
    assert proposal
    assert p.read_bytes() == before
    report = r.validate_registry()
    assert not report["ok"]


def test_import_existing_id_and_category_number(env):
    r, root, _ = env
    (root / "existing.md").write_text("---\nid: KB-000042\ncategory: RL\ncategory_number: RL-017\nyoutrack_issues: []\n---\n# 기존\n")
    d = r.allocate_document("existing.md")
    assert d["kb_id"] == "KB-000042"
    assert d["expected_frontmatter"]["category_number"] == "RL-017"
    assert r.allocate_document("existing.md")["kb_id"] == "KB-000042"
    new = r.allocate_document("new.md", category="RL")
    assert int(new["kb_id"].split("-")[1]) > 42
    assert int(new["expected_frontmatter"]["category_number"].split("-")[1]) > 17


def test_unregistered_ids_not_reissued(env):
    r, root, _ = env
    (root / "legacy.md").write_text("---\nid: KB-000001\n---\n# 기존\n")
    d = r.allocate_document("new.md")
    assert d["kb_id"] != "KB-000001"


def test_duplicate_existing_ids_detected(env):
    r, root, _ = env
    for name in ("a.md", "b.md"):
        (root / name).write_text("---\nid: KB-000099\n---\n# 중복\n")
    with pytest.raises(RegistryError):
        r.allocate_document("a.md")
    assert not r.validate_registry()["ok"]


def test_existing_category_collision(env):
    r, root, _ = env
    d = r.allocate_document("a.md", category="RL")
    write_document(root, d)
    (root / "b.md").write_text("---\nid: KB-000050\ncategory: RL\ncategory_number: RL-001\n---\n")
    with pytest.raises(RegistryError):
        r.allocate_document("b.md")


def test_multiple_youtrack_links_and_idempotency(env):
    r, root, _ = env
    d = r.allocate_document("a.md")
    write_document(root, d)
    r.confirm_document(d["kb_id"])
    r.link_youtrack(d["kb_id"], "MKR-1")
    r.link_youtrack(d["kb_id"], "MKR-2")
    result = r.link_youtrack(d["kb_id"], "MKR-1")
    assert result["expected_frontmatter"]["youtrack_issues"] == ["MKR-1", "MKR-2"]
    assert result["status"] == "pending"
    write_document(root, result)
    assert r.confirm_document(d["kb_id"])["status"] == "committed"


@pytest.mark.parametrize("path", ["../outside.md", "/etc/passwd", "a.txt", "", "a\x00.md"])
def test_invalid_paths(env, path):
    r, _, _ = env
    with pytest.raises(RegistryError):
        r.allocate_document(path)


def test_symlink_escape(env, tmp_path):
    r, root, _ = env
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.md").write_text("秘密")
    (root / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RegistryError):
        r.allocate_document("escape/x.md")


@pytest.mark.parametrize("category", ["bad code", "../../RL", "", "한글", "RL-001"])
def test_invalid_category(env, category):
    r, _, _ = env
    with pytest.raises(RegistryError):
        r.allocate_document("a.md", category=category)


@pytest.mark.parametrize("issue", ["bad", "MKR-0", "https://example.com", "", "MKR-1\n"])
def test_invalid_youtrack(env, issue):
    r, _, _ = env
    d = r.allocate_document("a.md")
    with pytest.raises(RegistryError):
        r.link_youtrack(d["kb_id"], issue)


@pytest.mark.parametrize("text", [
    "---\nid: KB-000001\nid: KB-000002\n---\n",
    "---\nid: [\n---\n",
    "---\n- invalid\n---\n",
    "---\nid: KB-000001\n",
    "---\nid: !!python/object:builtins.object {}\n---\n",
])
def test_invalid_yaml(env, text):
    r, root, _ = env
    (root / "a.md").write_text(text)
    with pytest.raises(RegistryError):
        r.allocate_document("a.md")
    assert not r.validate_registry()["ok"]


def test_missing_and_unregistered_documents(env):
    r, root, _ = env
    r.allocate_document("missing.md")
    (root / "unregistered.md").write_text("# 아직 미등록\n")
    assert not r.validate_registry()["ok"]


def test_root_binding(env, tmp_path):
    _, _, db = env
    another = tmp_path / "another"
    another.mkdir()
    with pytest.raises(RegistryError):
        Registry(db_path=db, kb_root=another)


def test_database_constraints(env):
    r, _, db = env
    r.allocate_document("a.md")
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_import_conflicts_with_pending_reservation(env):
    r, root, _ = env
    reserved = r.allocate_document("reserved.md")
    (root / "other.md").write_text(f"---\nid: {reserved['kb_id']}\n---\n")
    with pytest.raises(RegistryError):
        r.allocate_document("other.md")
    assert r.get_document(reserved["kb_id"])["path"] == "reserved.md"


def test_copy_not_treated_as_move(env):
    r, root, _ = env
    d = r.allocate_document("a.md")
    source = write_document(root, d)
    (root / "copy.md").write_bytes(source.read_bytes())
    with pytest.raises(RegistryError):
        r.update_document_path(d["kb_id"], "copy.md")


def test_registry_never_rewrites_existing_document(env):
    r, root, _ = env
    text = "# 사용자 원문\n\n$x \\in \\mathbb{R}$\n"
    target = root / "original.md"
    target.write_text(text)
    d = r.allocate_document("original.md", category="RL")
    r.assign_category(d["kb_id"], "ML")
    r.link_youtrack(d["kb_id"], "MKR-1")
    r.reconcile_document(d["kb_id"])
    r.validate_registry()
    assert target.read_text() == text


def test_successful_validation(env):
    r, root, _ = env
    assert r.validate_registry()["ok"]
    d = r.allocate_document("a.md")
    write_document(root, d)
    r.confirm_document(d["kb_id"])
    assert r.validate_registry()["ok"]
    (root / "a.md").unlink()
    assert not r.validate_registry()["ok"]


def test_atomic_failure_does_not_consume_document_id(env):
    r, _, _ = env
    with pytest.raises(RegistryError):
        r.allocate_document("a.md", category="bad code")
    assert r.allocate_document("b.md")["kb_id"] == "KB-000001"


def test_corrupted_database(tmp_path):
    root = tmp_path / "Documents"
    root.mkdir()
    db = tmp_path / "bad.sqlite3"
    db.write_bytes("SQLite가 아닌 손상 데이터".encode())
    with pytest.raises(RegistryError):
        Registry(db_path=db, kb_root=root)


def test_category_numbers_are_independent(env):
    r, _, _ = env
    a = r.allocate_document("rl.md", category="RL")
    b = r.allocate_document("ml.md", category="ML")
    assert a["expected_frontmatter"]["category_number"] == "RL-001"
    assert b["expected_frontmatter"]["category_number"] == "ML-001"
    assert a["kb_id"] != b["kb_id"]


def test_unknown_document_and_invalid_id(env):
    r, _, _ = env
    for kb_id in ("KB-999999", "invalid", "KB-000000", "KB-1", "KB-000001\n"):
        with pytest.raises(RegistryError):
            r.get_document(kb_id)


def test_unrelated_user_yaml_fields_are_allowed(env):
    r, root, _ = env
    d = r.allocate_document("a.qmd")
    meta = dict(d["expected_frontmatter"], title="제목", format="html", custom={"nested": 1})
    p = root / "a.qmd"
    p.write_text("---\n" + yaml.safe_dump(meta, allow_unicode=True) + "---\n$$x^2$$\n")
    before = p.read_bytes()
    assert r.confirm_document(d["kb_id"])["status"] == "committed"
    assert r.validate_registry()["ok"]
    assert p.read_bytes() == before


def test_absolute_path_inside_root(env):
    r, root, _ = env
    d = r.allocate_document(str(root / "inside.md"))
    assert d["path"] == "inside.md"


def test_unicode_paths(env):
    r, root, _ = env
    d = r.allocate_document("분류 없는 폴더/한글 문서.md")
    write_document(root, d)
    assert r.confirm_document(d["kb_id"])["status"] == "committed"


@pytest.mark.parametrize("frontmatter", [
    "id: KB-1\n",
    "id: KB-000000\n",
    "id: 123\n",
    "id: KB-000001\ncategory: RL\ncategory_number: ML-001\n",
    "id: KB-000001\ncategory: null\ncategory_number: RL-001\n",
    "id: KB-000001\nyoutrack_issues: not-a-list\n",
])
def test_invalid_existing_metadata(env, frontmatter):
    r, root, _ = env
    (root / "a.md").write_text("---\n" + frontmatter + "---\n")
    with pytest.raises(RegistryError):
        r.allocate_document("a.md")


def test_unregistered_category_numbers_reserved_before_import(env):
    r, root, _ = env
    (root / "legacy.md").write_text("---\nid: KB-000042\ncategory: RL\ncategory_number: RL-017\n---\n")
    fresh = r.allocate_document("fresh.md", category="RL")
    assert fresh["kb_id"] == "KB-000043"
    assert fresh["expected_frontmatter"]["category_number"] == "RL-018"


def test_duplicate_unregistered_category_numbers(env):
    r, root, _ = env
    for number, name in [(41, "a.md"), (42, "b.md")]:
        (root / name).write_text(f"---\nid: KB-{number:06d}\ncategory: RL\ncategory_number: RL-017\n---\n")
    with pytest.raises(RegistryError):
        r.allocate_document("fresh.md", category="RL")
    assert not r.validate_registry()["ok"]


def test_category_stable_identity_history_and_restart(env):
    r, root, db = env
    d = r.allocate_document("a.md", category="RL")
    write_document(root, d)
    r.confirm_document(d["kb_id"])
    before = r.list_categories()[0]
    after = r.rename_category("RL", "MARL")
    assert after["id"] == before["id"]
    assert after["aliases"] == ["MARL", "RL"]
    renamed = r.get_document(d["kb_id"])
    assert renamed["category_id"] == before["id"]
    assert renamed["expected_frontmatter"]["category_number"] == "MARL-001"
    assert renamed["status"] == "pending"
    write_document(root, renamed)
    r.confirm_document(d["kb_id"])
    r.delete_category("MARL")
    removed = r.get_document(d["kb_id"])
    assert removed["status"] == "pending"
    assert removed["category_id"] is None
    restarted = Registry(db_path=db, kb_root=root)
    category = restarted.list_categories()[0]
    assert category["id"] == before["id"] and not category["active"]
    for code in ["RL", "MARL"]:
        with pytest.raises(RegistryError):
            restarted.assign_category(d["kb_id"], code)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT display_id FROM category_reservations ORDER BY display_id").fetchall() == [("MARL-001",), ("RL-001",)]
        assert conn.execute("SELECT count(*) FROM history WHERE kb_id=?", (d["kb_id"],)).fetchone()[0] >= 5


@pytest.mark.parametrize("field,value", [
    ("id", "KB-000999"), ("category", "OTHER"),
    ("category_number", "RL-999"), ("youtrack_issues", ["MKR-999"]),
])
def test_yaml_tamper_never_overwrites_registry(env, field, value):
    r, root, _ = env
    d = r.allocate_document("a.md", category="RL")
    d = r.link_youtrack(d["kb_id"], "MKR-1")
    write_document(root, d)
    d = r.confirm_document(d["kb_id"])
    expected = dict(d["expected_frontmatter"])
    tampered = dict(d, expected_frontmatter=dict(expected, **{field: value}))
    write_document(root, tampered)
    r.reconcile_document(d["kb_id"])
    assert not r.validate_registry()["ok"]
    with pytest.raises(RegistryError):
        r.confirm_document(d["kb_id"])
    actual = r.get_document(d["kb_id"])
    assert actual["expected_frontmatter"] == expected
    assert actual["status"] == "pending"


def test_registered_yaml_cannot_poison_counters(env):
    r, root, _ = env
    d = r.allocate_document("a.md", category="RL")
    write_document(root, d)
    r.confirm_document(d["kb_id"])
    write_document(root, dict(d, expected_frontmatter=dict(d["expected_frontmatter"], category_number="RL-999")))
    fresh = r.allocate_document("fresh.md", category="RL")
    assert fresh["expected_frontmatter"]["category_number"] == "RL-002"
    assert r.get_document(d["kb_id"])["expected_frontmatter"]["category_number"] == "RL-001"


def test_confirm_rejects_duplicate_category_number(env):
    r, root, _ = env
    d = r.allocate_document("a.md", category="RL")
    write_document(root, d)
    (root / "other.md").write_text("---\nid: KB-000042\ncategory: RL\ncategory_number: RL-001\n---\n")
    with pytest.raises(RegistryError):
        r.confirm_document(d["kb_id"])
    assert r.get_document(d["kb_id"])["status"] == "pending"


def test_registered_file_replaced_by_external_symlink(env, tmp_path):
    r, root, _ = env
    d = r.allocate_document("a.md")
    p = write_document(root, d)
    r.confirm_document(d["kb_id"])
    outside = tmp_path / "private.md"
    secret = "노출되어서는 안 되는 외부 문서 내용"
    outside.write_text(secret)
    p.unlink()
    p.symlink_to(outside)
    for report in (r.validate_registry(), r.reconcile_document(d["kb_id"])):
        assert secret not in str(report)
    with pytest.raises(RegistryError):
        r.confirm_document(d["kb_id"])
    with pytest.raises(RegistryError):
        r.update_document_path(d["kb_id"], str(outside))
    with pytest.raises(RegistryError):
        r.reconcile_document(d["kb_id"], str(outside))
    assert outside.read_text() == secret


def test_idempotency_survives_restart_and_category_retry_is_noop(env):
    r, root, db = env
    d = r.allocate_document("a.md", category="RL", idempotency_key="retry")
    restarted = Registry(db_path=db, kb_root=root)
    again = restarted.allocate_document("a.md", category="ML", idempotency_key="retry")
    # allocate의 경로·키는 동일 문서를 식별하며 분류 변경은 assign_category만 담당한다.
    assert again["expected_frontmatter"] == d["expected_frontmatter"]
    assert [c["code"] for c in restarted.list_categories()] == ["RL"]


def test_forty_seven_documents_never_modified(env):
    r, root, _ = env
    for i in range(47):
        p = root / f"folder-{i % 3}" / f"문서 {i}.md"
        p.parent.mkdir(exist_ok=True)
        p.write_text(f"# 원문 {i}\n\n$\\alpha + {i}$\n")
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    for i in range(3):
        d = r.allocate_document(f"folder-{i % 3}/문서 {i}.md", category="RL")
        with pytest.raises(RegistryError):
            r.confirm_document(d["kb_id"])
        r.reconcile_document(d["kb_id"])
    r.validate_registry()
    after = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert after == before


def test_validate_detects_semantic_db_corruption(env):
    r, _, db = env
    d = r.allocate_document("a.md", category="RL")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE categories SET next_number=1")
    report = r.validate_registry()
    assert not report["ok"]
    assert any(issue.get("check") == "category_counter_behind" for issue in report["issues"])
