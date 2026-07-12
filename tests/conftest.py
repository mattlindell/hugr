"""
Patch heavy dependencies before any source module is imported.
This lets us run tests without a GPU, running Docker, or network access.
"""
import sys
from unittest.mock import MagicMock

# --- sentence_transformers ---
mock_st_module = MagicMock()
mock_model = MagicMock()
mock_model.encode.return_value = [0.0] * 768  # fake 768-dim embedding
mock_st_module.SentenceTransformer.return_value = mock_model
sys.modules.setdefault("sentence_transformers", mock_st_module)

# --- pgvector ---
mock_pgvector = MagicMock()
sys.modules.setdefault("pgvector", mock_pgvector)
sys.modules.setdefault("pgvector.psycopg2", mock_pgvector)

# --- psycopg2 (full tree) ---
mock_psycopg2 = MagicMock()
sys.modules.setdefault("psycopg2", mock_psycopg2)
sys.modules.setdefault("psycopg2.extras", mock_psycopg2.extras)
sys.modules.setdefault("psycopg2.pool", mock_psycopg2.pool)

# --- openai ---
mock_openai = MagicMock()
sys.modules.setdefault("openai", mock_openai)

# --- starlette ---
mock_starlette = MagicMock()
sys.modules.setdefault("starlette", mock_starlette)
sys.modules.setdefault("starlette.requests", mock_starlette.requests)
sys.modules.setdefault("starlette.responses", mock_starlette.responses)

# --- mcp ---
mock_mcp = MagicMock()
mock_fastmcp = MagicMock()
mock_fastmcp_instance = MagicMock()
# Make @mcp.tool() and @mcp.custom_route() pass-through decorators
mock_fastmcp_instance.tool.return_value = lambda f: f
mock_fastmcp_instance.custom_route.return_value = lambda f: f
mock_fastmcp.FastMCP.return_value = mock_fastmcp_instance
sys.modules.setdefault("mcp", mock_mcp)
sys.modules.setdefault("mcp.server", mock_mcp)
sys.modules.setdefault("mcp.server.fastmcp", mock_fastmcp)
