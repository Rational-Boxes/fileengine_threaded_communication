"""Build FileEngine gRPC clients (``ManagedFiles``) bound to a specific identity.

The trusted-upstream model: whatever user/roles/tenant we pass is sent verbatim in
every request's ``AuthenticationContext``, and the core enforces ACLs against it.

Unlike CSAI, this service does **not** index foreign content, so there is no
"read everything" agent: every user-facing check runs as the end user via
``client_for(identity)``. ``agent_client`` exists only for internal maintenance
that legitimately acts as the service account (never to answer a user request, and
never with an ACL bypass — per the impersonation rule, SPECIFICATION §5).

``fileengine`` is imported lazily here so config/auth/health import without the
gRPC stack; import this module only where a core call is actually made (M1+).
"""
from .ldap_auth import Identity, authenticate


def client_for(identity: Identity, config):
    """A gRPC client that acts as ``identity`` (the end user)."""
    from ._client import ManagedFiles
    return ManagedFiles(
        server_address=config.grpc_address,
        user_name=identity.user,
        user_roles=identity.roles,
        tenant=identity.tenant or config.tenant,
    )


def agent_identity(config) -> Identity:
    """Authenticate the service's own agent account against LDAP."""
    return authenticate(config, config.agent_user, config.agent_password)


def agent_client(config):
    """A gRPC client acting as this service's agent identity (internal maintenance
    only — never a user-facing read, and no ACL bypass)."""
    return client_for(agent_identity(config), config)
