"""P1：模型结构对齐 ADR（无需 DB，检查 metadata）。"""

from pgvector.sqlalchemy import Vector

from app.db.base import Base
import app.models  # noqa: F401

MD = Base.metadata


def _cols(table: str) -> dict:
    return {c.name: c for c in MD.tables[table].columns}


def test_table_set_is_10():
    assert set(MD.tables) == {
        "users",
        "prompt_templates",
        "ai_operation_records",
        "conversations",
        "messages",
        "long_term_memories",
        "documents",
        "knowledge_chunks",
        "ai_readme_documents",
        "agent_runs",  # ADR-0017 LLMOps run trace
    }


def test_knowledge_chunk_inline_vector_and_fk_cascade():
    cols = _cols("knowledge_chunks")
    assert "document_id" in cols
    assert isinstance(cols["embedding"].type, Vector)
    assert cols["embedding"].type.dim == 1024
    fks = MD.tables["knowledge_chunks"].foreign_keys
    assert any(
        fk.column.table.name == "documents" and fk.ondelete == "CASCADE" for fk in fks
    )


def test_long_term_memory_inline_vector():
    cols = _cols("long_term_memories")
    assert isinstance(cols["embedding"].type, Vector)
    assert cols["embedding"].type.dim == 1024
    assert "metadata" in cols  # 列名 metadata（属性 meta）


def test_ai_operation_record_merged_shape():
    cols = _cols("ai_operation_records")
    for c in ["type", "project_name", "file_path", "source_code", "result", "metadata"]:
        assert c in cols


def test_conversations_summary_and_conversation_id():
    cols = _cols("conversations")
    assert "conversation_id" in cols
    assert cols["conversation_id"].unique
    assert "summary" in cols  # ADR-0003 摘要持久化
    assert "summary_message_count" in cols
    assert cols["summary_message_count"].nullable is False
    assert str(cols["summary_message_count"].server_default.arg) == "0"


def test_messages_fk_to_conversation_cascade():
    fks = MD.tables["messages"].foreign_keys
    assert any(
        fk.column.table.name == "conversations"
        and fk.column.name == "conversation_id"
        and fk.ondelete == "CASCADE"
        for fk in fks
    )


def test_prompt_template_version_and_active():
    cols = _cols("prompt_templates")
    assert "version" in cols
    assert "is_active" in cols


def test_documents_stores_full_content_once():
    cols = _cols("documents")
    assert "content" in cols  # 父表存全文一次（ADR-0002）


def test_ai_readme_snapshot_metadata_and_project_version_uniqueness():
    table = MD.tables["ai_readme_documents"]
    cols = _cols("ai_readme_documents")
    for name in [
        "snapshot_hash",
        "snapshot_file_count",
        "snapshot_generated_at",
        "snapshot_truncated",
    ]:
        assert name in cols
        assert cols[name].nullable is True

    assert cols["snapshot_hash"].type.length == 64
    indexes = {index.name: index for index in table.indexes}
    project_version = indexes["uq_ard_project_name_version"]
    assert project_version.unique is True
    assert [column.name for column in project_version.columns] == [
        "project_name",
        "version",
    ]
