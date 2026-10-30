"""Optional integrations. Nothing here is imported by ``redline_core`` itself.

Each module imports its third-party library at import time, so import only what you use:

- ``redline_core.contrib.sqlalchemy``: engine instrumentation, EXPLAIN executor, fix suggester
- ``redline_core.contrib.boto3``: AWS SDK operation spans (no argument/response capture)
- ``redline_core.contrib.httpx``: outbound ``httpx`` calls as ``http_out`` spans
- ``redline_core.contrib.requests``: outbound ``requests`` calls as ``http_out`` spans
- ``redline_core.contrib.redis``: redis-py (sync and asyncio) commands as ``cache`` spans
- ``redline_core.contrib.asyncpg``: asyncpg connections as ``sql`` spans
- ``redline_core.contrib.psycopg``: psycopg 3 cursors as ``sql`` spans
- ``redline_core.contrib.strawberry``: Strawberry ``RedlineExtension`` (GraphQL resolver spans)
- ``redline_core.contrib.graphene``: Graphene ``RedlineMiddleware`` (GraphQL resolver spans)
"""
