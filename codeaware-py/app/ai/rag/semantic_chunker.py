"""SemanticChunker - unstructured chunk_by_title（P3-3 分块策略，结构感知+格式无关）。

parse(Markdown/文本) -> elements -> chunk_by_title(按 title 切 + 控大小 + overlap)。
仅作用于 Knowledge 文档（ADR-0002）；Long-term Memory 原子不分块（ADR-0001）。
"""

from unstructured.chunking.title import chunk_by_title
from unstructured.partition.md import partition_md
from unstructured.partition.text import partition_text

from app.core.config import settings


class SemanticChunker:
    def __init__(self, chunk_size: int | None = None, overlap: int | None = None) -> None:
        # None → 用配置默认（.env 可调）；显式传入可覆盖（测试/评估用）
        self.chunk_size = chunk_size if chunk_size is not None else settings.rag_chunk_size
        self.overlap = overlap if overlap is not None else settings.rag_chunk_overlap

    def chunk(self, content: str, content_type: str = "md") -> list[str]:
        """按 Markdown 标题切（结构感知）+ 控大小 + overlap，返回 chunk 文本列表。"""
        if not content or not content.strip():
            return []
        elements = (
            partition_md(text=content)
            if content_type == "md"
            else partition_text(text=content)
        )
        chunks = chunk_by_title(
            elements,
            max_characters=self.chunk_size,
            new_after_n_chars=self.chunk_size,
            overlap=self.overlap,
            combine_text_under_n_chars=0,  # 不合并小节，按标题切（匹配 Java 行为）
        )
        return [c.text for c in chunks if c.text and c.text.strip()]
