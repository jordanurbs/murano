"""Pydantic schemas for `/api/v1/*` request and response bodies.

Kept deliberately narrow — these are the wire types only. Internal types
(`RetrievalResult`, `CapturedPage`, etc.) live next to their producers.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    query: str = Field(..., min_length=1, description="The user's question.")
    k: int = Field(6, ge=1, le=20, description="Top-K chunks to retrieve as context.")
    max_tokens: int = Field(1024, ge=16, le=8192)
    temperature: float = Field(0.2, ge=0.0, le=2.0)
    include_summaries: bool = True
    summary_k: int = Field(2, ge=0, le=10)
    summary_level: int = Field(1, ge=1, le=6)


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    k: int = Field(10, ge=1, le=50)


class CaptureRequest(BaseModel):
    url: str = Field(..., min_length=1)
    tags: list[str] = Field(default_factory=list)


class OpenRequest(BaseModel):
    path: str = Field(..., min_length=1, description="Vault-relative path to open.")


class ChunkResponse(BaseModel):
    chunk_id: str
    file_path: str
    ord: int
    heading_path: str
    content: str
    token_count: int
    byte_offset: int


class SearchHitResponse(BaseModel):
    rank: int
    chunk_id: str
    file_path: str
    heading_path: str
    citation: str
    distance: float
    token_count: int
    content: str


class SearchResponse(BaseModel):
    query: str
    embed_model: str
    elapsed_ms: float
    hits: list[SearchHitResponse]


class ThemeResponse(BaseModel):
    id: str
    level: int
    title: str
    summary: str
    member_count: int
    parent_id: str | None


class ThemesResponse(BaseModel):
    level: int
    themes: list[ThemeResponse]


class CapturedResponse(BaseModel):
    url: str
    title: str
    relpath: str
    absolute_path: str
    word_count: int
    byte_count: int
    site_name: str | None
    published_date: str | None
    chunks_indexed: int


class HealthResponse(BaseModel):
    status: str
    version: str
    vault_root: str
    data_root: str
    chunks_db_exists: bool
    summary_tree_exists: bool
    chunk_count: int
    file_count: int
    tree_node_count: int
    tree_stale: bool
    tree_stale_reason: str | None
    api_key_present: bool  # legacy: True iff keychain has a Venice key
    api_key_source: str  # "keychain" | "env" | "none" — the effective key source
    chat_model: str
    embed_model: str
    venice_base_url: str
