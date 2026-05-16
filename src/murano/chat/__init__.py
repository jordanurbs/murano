"""Chat: shared retrieval + RAG answer pipeline.

This package is the single retrieval/generation core used by all three
transports (CLI, MCP, HTTP). Phase 3 ships the flat-RAG version; Phase 5
adds hierarchical (summary-tree) retrieval behind the same interface.
"""
