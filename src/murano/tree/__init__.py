"""Hierarchical summary tree (the "memory tree").

Builds a multi-level cluster summary tree on top of the chunks index. The
tree lets thematic queries ("what are my main interests in X") drill down
from a small set of high-level themes to the specific chunks that
contributed to them, and gives the RAG prompt extra context without
inflating the citation set.
"""
