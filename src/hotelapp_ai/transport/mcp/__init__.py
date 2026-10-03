"""MCP protocol edges -- tool definitions, and one entry point per transport.

AI Step 5 builds `tools.py` and `stdio.py` only. `http.py` and `resource_metadata.py` are the
full, OAuth-gated surface and are a later step (phased-implementation-plan.md's Phase 9 item
table).
"""

from __future__ import annotations
