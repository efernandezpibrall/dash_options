"""Fail-closed authorization primitives for future calibration writes."""

from __future__ import annotations

import hmac
import ipaddress
import os
from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from runtime_config import config_value


class AuthenticationError(RuntimeError):
    """Raised when trusted-proxy authentication cannot be verified."""


class AuthorizationError(PermissionError):
    """Raised when an authenticated identity lacks a required permission."""


class Role(str, Enum):
    VIEWER = "viewer"
    CALIBRATOR = "calibrator"
    APPROVER = "approver"
    PUBLISHER = "publisher"


class Permission(str, Enum):
    VIEW = "view"
    CREATE_DRAFT = "create_draft"
    SUBMIT = "submit"
    APPROVE = "approve"
    REJECT = "reject"
    PUBLISH = "publish"
    SUPERSEDE = "supersede"
    CANCEL_JOB = "cancel_job"
    REFRESH_BLOOMBERG = "refresh_bloomberg"


ROLE_PERMISSIONS = {
    Role.VIEWER: frozenset({Permission.VIEW}),
    Role.CALIBRATOR: frozenset(
        {
            Permission.VIEW,
            Permission.CREATE_DRAFT,
            Permission.SUBMIT,
            Permission.CANCEL_JOB,
            Permission.REFRESH_BLOOMBERG,
        }
    ),
    Role.APPROVER: frozenset(
        {
            Permission.VIEW,
            Permission.APPROVE,
            Permission.REJECT,
            Permission.PUBLISH,
            Permission.SUPERSEDE,
        }
    ),
    Role.PUBLISHER: frozenset(
        {
            Permission.VIEW,
            Permission.PUBLISH,
            Permission.SUPERSEDE,
        }
    ),
}


@dataclass(frozen=True)
class Identity:
    subject: str | None
    roles: frozenset[Role]
    authenticated: bool
    auth_source: str

    @classmethod
    def anonymous(cls):
        return cls(
            subject=None,
            roles=frozenset(),
            authenticated=False,
            auth_source="disabled",
        )


def _env_enabled(environ: Mapping[str, str], name: str) -> bool:
    return environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _setting(
    environ: Mapping[str, str],
    name: str,
    option: str,
    *,
    use_runtime_config: bool,
    default: str = "",
) -> str:
    if name in environ:
        return str(environ[name])
    if use_runtime_config:
        return str(config_value("OPTIONS_AUTH", option, fallback=default) or default)
    return default


def _parse_roles(raw_roles: str) -> frozenset[Role]:
    parsed_roles = set()
    for raw_role in raw_roles.split(","):
        normalized = raw_role.strip().lower()
        if not normalized:
            continue
        try:
            parsed_roles.add(Role(normalized))
        except ValueError as exc:
            raise AuthenticationError(
                f"Unsupported authorization role: {normalized!r}"
            ) from exc
    return frozenset(parsed_roles)


def configured_auth_mode(
    *, environ: Mapping[str, str] | None = None
) -> str:
    """Return the explicit server-side authentication mode."""
    env = os.environ if environ is None else environ
    mode = _setting(
        env,
        "OPTIONS_AUTH_MODE",
        "MODE",
        use_runtime_config=environ is None,
        default="",
    ).strip().lower()
    if mode:
        return mode
    if _env_enabled(env, "OPTIONS_TRUSTED_PROXY_AUTH_ENABLED"):
        return "trusted_proxy"
    return "disabled"


def validate_auth_configuration(
    *, environ: Mapping[str, str] | None = None
) -> str:
    """Validate auth settings without resolving a request identity."""
    env = os.environ if environ is None else environ
    use_runtime_config = environ is None
    mode = configured_auth_mode(environ=environ)
    if mode == "disabled":
        raise AuthenticationError("Authentication is disabled.")
    if mode == "local_loopback":
        subject = _setting(
            env,
            "OPTIONS_LOCAL_AUTH_USER",
            "LOCAL_USER",
            use_runtime_config=use_runtime_config,
        ).strip()
        roles = _parse_roles(
            _setting(
                env,
                "OPTIONS_LOCAL_AUTH_ROLES",
                "LOCAL_ROLES",
                use_runtime_config=use_runtime_config,
            )
        )
        if not subject:
            raise AuthenticationError("Local-loopback authentication requires a user.")
        if not roles:
            raise AuthenticationError(
                "Local-loopback authentication requires at least one role."
            )
        return mode
    if mode == "trusted_proxy":
        secret = _setting(
            env,
            "OPTIONS_TRUSTED_PROXY_SHARED_SECRET",
            "TRUSTED_PROXY_SHARED_SECRET",
            use_runtime_config=use_runtime_config,
        )
        if not secret:
            raise AuthenticationError(
                "Trusted proxy authentication is enabled without a shared secret."
            )
        return mode
    raise AuthenticationError(f"Unsupported authentication mode: {mode!r}")


def resolve_trusted_proxy_identity(
    headers: Mapping[str, str],
    *,
    environ: Mapping[str, str] | None = None,
) -> Identity:
    """Resolve identity only when proxy auth and its shared secret are configured."""
    env = os.environ if environ is None else environ
    use_runtime_config = environ is None
    if configured_auth_mode(environ=environ) != "trusted_proxy":
        return Identity.anonymous()

    configured_secret = _setting(
        env,
        "OPTIONS_TRUSTED_PROXY_SHARED_SECRET",
        "TRUSTED_PROXY_SHARED_SECRET",
        use_runtime_config=use_runtime_config,
    )
    if not configured_secret:
        raise AuthenticationError(
            "Trusted proxy authentication is enabled without a shared secret."
        )

    secret_header = _setting(
        env,
        "OPTIONS_TRUSTED_PROXY_SECRET_HEADER",
        "TRUSTED_PROXY_SECRET_HEADER",
        use_runtime_config=use_runtime_config,
        default="X-Options-Proxy-Secret",
    )
    supplied_secret = headers.get(secret_header, "")
    if not supplied_secret or not hmac.compare_digest(configured_secret, supplied_secret):
        raise AuthenticationError("Trusted proxy authentication failed.")

    user_header = _setting(
        env,
        "OPTIONS_TRUSTED_PROXY_USER_HEADER",
        "TRUSTED_PROXY_USER_HEADER",
        use_runtime_config=use_runtime_config,
        default="X-Forwarded-User",
    )
    roles_header = _setting(
        env,
        "OPTIONS_TRUSTED_PROXY_ROLES_HEADER",
        "TRUSTED_PROXY_ROLES_HEADER",
        use_runtime_config=use_runtime_config,
        default="X-Forwarded-Roles",
    )
    subject = headers.get(user_header, "").strip()
    if not subject:
        raise AuthenticationError("Trusted proxy did not supply an authenticated user.")

    parsed_roles = _parse_roles(headers.get(roles_header, ""))

    return Identity(
        subject=subject,
        roles=parsed_roles,
        authenticated=True,
        auth_source="trusted_proxy",
    )


def resolve_request_identity(
    headers: Mapping[str, str],
    *,
    remote_addr: str | None,
    environ: Mapping[str, str] | None = None,
) -> Identity:
    """Resolve trusted-proxy or explicitly local workstation identity."""
    env = os.environ if environ is None else environ
    use_runtime_config = environ is None
    mode = configured_auth_mode(environ=environ)
    if mode == "disabled":
        return Identity.anonymous()
    if mode == "trusted_proxy":
        return resolve_trusted_proxy_identity(headers, environ=environ)
    validate_auth_configuration(environ=environ)
    if any(
        headers.get(name)
        for name in ("Forwarded", "X-Forwarded-For", "X-Real-IP")
    ):
        raise AuthenticationError(
            "Local-loopback authentication rejects forwarded requests."
        )
    try:
        is_loopback = bool(remote_addr) and ipaddress.ip_address(
            remote_addr
        ).is_loopback
    except ValueError as exc:
        raise AuthenticationError("Local-loopback request address is invalid.") from exc
    if not is_loopback:
        raise AuthenticationError(
            "Local-loopback authentication requires a loopback request."
        )
    subject = _setting(
        env,
        "OPTIONS_LOCAL_AUTH_USER",
        "LOCAL_USER",
        use_runtime_config=use_runtime_config,
    ).strip()
    roles = _parse_roles(
        _setting(
            env,
            "OPTIONS_LOCAL_AUTH_ROLES",
            "LOCAL_ROLES",
            use_runtime_config=use_runtime_config,
        )
    )
    return Identity(
        subject=subject,
        roles=roles,
        authenticated=True,
        auth_source="local_loopback",
    )


def authorize(
    identity: Identity,
    permission: Permission,
    *,
    resource_creator: str | None = None,
) -> None:
    if not identity.authenticated or not identity.subject:
        raise AuthorizationError("Authentication is required.")

    granted = set()
    for role in identity.roles:
        granted.update(ROLE_PERMISSIONS[role])
    if permission not in granted:
        raise AuthorizationError(f"Permission {permission.value!r} is required.")

    if (
        permission in {Permission.APPROVE, Permission.REJECT}
        or (permission is Permission.PUBLISH and Role.PUBLISHER not in identity.roles)
    ) and (
        resource_creator
        and hmac.compare_digest(identity.subject, resource_creator)
    ):
        raise AuthorizationError("A calibrator cannot approve or publish their own run.")
