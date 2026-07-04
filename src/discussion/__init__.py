"""Discussion & threaded communication service.

A thin FastAPI door over the canonical FileEngine gRPC core: document-anchored
threads and review requests, permission-enforced *as the end user*, per-tenant
Postgres, event-driven. See SPECIFICATION.md.
"""

__version__ = "0.1.0"
