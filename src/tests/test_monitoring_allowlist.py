"""Route-scoped monitoring IP allowlist (security review L2).

The unauthenticated /healthz|/readyz|/poolz endpoints may be guarded by
FILEENGINE_MONITORING_ALLOW_IPS. The guard must:
  - reject a monitoring request from a non-listed client IP (403),
  - permit a listed client IP,
  - NEVER gate non-monitoring (real API) paths — it is route-scoped,
  - be a no-op when the allowlist is unset.
Starlette's TestClient reports the client host as "testclient".
"""
import os

from fastapi.testclient import TestClient

from discussion.app import build_app


def _client(allow):
    if allow is None:
        os.environ.pop("FILEENGINE_MONITORING_ALLOW_IPS", None)
    else:
        os.environ["FILEENGINE_MONITORING_ALLOW_IPS"] = allow
    return TestClient(build_app())


def test_blocks_non_listed_ip():
    assert _client("10.9.9.9").get("/healthz").status_code == 403


def test_permits_listed_ip():
    assert _client("testclient").get("/healthz").status_code != 403


def test_is_route_scoped():
    # A non-monitoring path is never gated by the allowlist (404/401, never 403).
    assert _client("10.9.9.9").get("/v1/definitely-not-a-route").status_code != 403


def test_no_allowlist_allows_all():
    assert _client(None).get("/healthz").status_code != 403
