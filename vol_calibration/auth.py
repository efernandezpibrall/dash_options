"""Fail-closed authorization primitives for future calibration writes."""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from enum import Enum
from typing import Mapping


class AuthenticationError(RuntimeError):
    """Raised when trusted-proxy authentication cannot be verified."""


class AuthorizationError(PermissionError):
    """Raised when an authenticated identity lacks a required permission."""


class Role(str, Enum):
    VIEWER = "viewer"
    CALIBRATOR = "calibrator"
    APPROVER = "approver"


class Permission(str, Enum):
    VIEW = "view"
    CREATE_DRAFT = "create_draft"
    SUBMIT = "submit"
    APPROVE = "approve"
    REJECT = "reject"
    PUBLISH = "publish"
    SUPERSEDE = "supersede"
    CANCEL_JOB = "cancel_job"


ROLE_PERMISSIONS = {
    Role.VIEWER: frozenset({Permission.VIEW}),
    Role.CALIBRATOR: frozenset(
        {
            Permission.VIEW,
            Permission.CREATE_DRAFT,
            Permission.SUBMIT,
            Permission.CANCEL_JOB,
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


def resolve_trusted_proxy_identity(
    headers: Mapping[str, str],
    *,
    environ: Mapping[str, str] | None = None,
) -> Identity:
    """Resolve identity only when proxy auth and its shared secret are configured."""
    env = os.environ if environ is None else environ
    if not _env_enabled(env, "OPTIONS_TRUSTED_PROXY_AUTH_ENABLED"):
        return Identity.anonymous()

    configured_secret = env.get("OPTIONS_TRUSTED_PROXY_SHARED_SECRET", "")
    if not configured_secret:
        raise AuthenticationError(
            "Trusted proxy authentication is enabled without a shared secret."
        )

    secret_header = env.get("OPTIONS_TRUSTED_PROXY_SECRET_HEADER", "X-Options-Proxy-Secret")
    supplied_secret = headers.get(secret_header, "")
    if not supplied_secret or not hmac.compare_digest(configured_secret, supplied_secret):
        raise AuthenticationError("Trusted proxy authentication failed.")

    user_header = env.get("OPTIONS_TRUSTED_PROXY_USER_HEADER", "X-Forwarded-User")
    roles_header = env.get("OPTIONS_TRUSTED_PROXY_ROLES_HEADER", "X-Forwarded-Roles")
    subject = headers.get(user_header, "").strip()
    if not subject:
        raise AuthenticationError("Trusted proxy did not supply an authenticated user.")

    parsed_roles = set()
    for raw_role in headers.get(roles_header, "").split(","):
        normalized = raw_role.strip().lower()
        if not normalized:
            continue
        try:
            parsed_roles.add(Role(normalized))
        except ValueError as exc:
            raise AuthenticationError(f"Unsupported proxy role: {normalized!r}") from exc

    return Identity(
        subject=subject,
        roles=frozenset(parsed_roles),
        authenticated=True,
        auth_source="trusted_proxy",
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
        permission in {Permission.APPROVE, Permission.REJECT, Permission.PUBLISH}
        and resource_creator
        and hmac.compare_digest(identity.subject, resource_creator)
    ):
        raise AuthorizationError("A calibrator cannot approve or publish their own run.")
