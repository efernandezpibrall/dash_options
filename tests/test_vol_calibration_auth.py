import pytest

from vol_calibration.auth import (
    AuthenticationError,
    AuthorizationError,
    Identity,
    Permission,
    Role,
    authorize,
    resolve_request_identity,
    resolve_trusted_proxy_identity,
)


def test_trusted_proxy_auth_is_disabled_and_default_denies():
    identity = resolve_trusted_proxy_identity({}, environ={})

    assert identity == Identity.anonymous()
    with pytest.raises(AuthorizationError, match="Authentication is required"):
        authorize(identity, Permission.CREATE_DRAFT)


def test_enabled_proxy_auth_requires_a_configured_and_matching_secret():
    enabled_without_secret = {"OPTIONS_TRUSTED_PROXY_AUTH_ENABLED": "true"}
    with pytest.raises(AuthenticationError, match="without a shared secret"):
        resolve_trusted_proxy_identity({}, environ=enabled_without_secret)

    environment = {
        "OPTIONS_TRUSTED_PROXY_AUTH_ENABLED": "true",
        "OPTIONS_TRUSTED_PROXY_SHARED_SECRET": "expected-secret",
    }
    with pytest.raises(AuthenticationError, match="authentication failed"):
        resolve_trusted_proxy_identity(
            {"X-Options-Proxy-Secret": "untrusted"},
            environ=environment,
        )


def test_proxy_roles_are_authorized_server_side():
    environment = {
        "OPTIONS_TRUSTED_PROXY_AUTH_ENABLED": "yes",
        "OPTIONS_TRUSTED_PROXY_SHARED_SECRET": "expected-secret",
    }
    identity = resolve_trusted_proxy_identity(
        {
            "X-Options-Proxy-Secret": "expected-secret",
            "X-Forwarded-User": "calibrator@example.com",
            "X-Forwarded-Roles": "viewer, calibrator",
        },
        environ=environment,
    )

    assert identity.authenticated is True
    assert identity.roles == frozenset({Role.VIEWER, Role.CALIBRATOR})
    authorize(identity, Permission.CREATE_DRAFT)
    authorize(identity, Permission.CANCEL_JOB)
    with pytest.raises(AuthorizationError, match="approve"):
        authorize(identity, Permission.APPROVE)


def test_self_approval_and_self_publication_are_rejected():
    identity = Identity(
        subject="owner@example.com",
        roles=frozenset({Role.APPROVER}),
        authenticated=True,
        auth_source="trusted_proxy",
    )

    for permission in (Permission.APPROVE, Permission.REJECT, Permission.PUBLISH):
        with pytest.raises(AuthorizationError, match="their own run"):
            authorize(
                identity,
                permission,
                resource_creator="owner@example.com",
            )


def test_unknown_proxy_roles_fail_closed():
    environment = {
        "OPTIONS_TRUSTED_PROXY_AUTH_ENABLED": "true",
        "OPTIONS_TRUSTED_PROXY_SHARED_SECRET": "expected-secret",
    }
    with pytest.raises(AuthenticationError, match="Unsupported authorization role"):
        resolve_trusted_proxy_identity(
            {
                "X-Options-Proxy-Secret": "expected-secret",
                "X-Forwarded-User": "user@example.com",
                "X-Forwarded-Roles": "administrator",
            },
            environ=environment,
        )


def test_local_loopback_identity_is_server_configured_and_fail_closed():
    environment = {
        "OPTIONS_AUTH_MODE": "local_loopback",
        "OPTIONS_LOCAL_AUTH_USER": "trader@example.com",
        "OPTIONS_LOCAL_AUTH_ROLES": "calibrator,publisher",
    }
    identity = resolve_request_identity(
        {}, remote_addr="127.0.0.1", environ=environment
    )

    assert identity.subject == "trader@example.com"
    assert identity.roles == frozenset({Role.CALIBRATOR, Role.PUBLISHER})
    assert identity.auth_source == "local_loopback"
    authorize(
        identity,
        Permission.PUBLISH,
        resource_creator="trader@example.com",
    )

    with pytest.raises(AuthenticationError, match="loopback request"):
        resolve_request_identity(
            {}, remote_addr="10.0.0.10", environ=environment
        )
    with pytest.raises(AuthenticationError, match="rejects forwarded"):
        resolve_request_identity(
            {"X-Forwarded-For": "127.0.0.1"},
            remote_addr="127.0.0.1",
            environ=environment,
        )


def test_publisher_does_not_bypass_self_approval_policy():
    identity = Identity(
        subject="trader@example.com",
        roles=frozenset({Role.CALIBRATOR, Role.PUBLISHER, Role.APPROVER}),
        authenticated=True,
        auth_source="local_loopback",
    )

    with pytest.raises(AuthorizationError, match="their own run"):
        authorize(
            identity,
            Permission.APPROVE,
            resource_creator="trader@example.com",
        )
