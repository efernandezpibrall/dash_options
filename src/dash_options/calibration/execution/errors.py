"""Execution failure classification and bounded, credential-free owner feedback."""

import re

import psycopg2
from sqlalchemy import exc as sa_exc


def is_database_error(error):
    return isinstance(error, (psycopg2.Error, sa_exc.SQLAlchemyError))


def retryable_execution(error):
    """Retry transient DB failures, including raw COPY errors outside SQLAlchemy.

    A SQLSTATE wins over the broad OperationalError class: missing objects,
    permissions, integrity and data failures need correction rather than retry.
    """
    original = getattr(error, "orig", error)
    state = getattr(original, "pgcode", None) or getattr(original, "sqlstate", None)
    if state:
        return (state[:2] in {"08", "40", "53"}
                or state in {"55P03", "57014", "57P01", "57P02", "57P03", "58030"})
    return (isinstance(error, (sa_exc.TimeoutError, sa_exc.OperationalError, sa_exc.InterfaceError))
            or isinstance(original, (psycopg2.OperationalError, psycopg2.InterfaceError))
            or bool(getattr(error, "connection_invalidated", False)))


def safe_failure_reason(error):
    """Do not render driver SQL, parameters, connection details or credentials."""
    if isinstance(error, BaseException) and is_database_error(error):
        original = getattr(error, "orig", error)
        state = getattr(original, "pgcode", None) or getattr(original, "sqlstate", None)
        state = f" (SQLSTATE {state})" if state and re.fullmatch(r"[0-9A-Z]{5}", state) else ""
        action = "temporary database failure; retry available" if retryable_execution(error) else "database operation requires review"
        return f"{type(error).__name__}{state}: {action}"
    value = str(error)
    value = re.sub(r"\b[a-z][a-z0-9+.-]*://\S+", "[redacted URL]", value, flags=re.I)
    value = re.sub(r"\b(password|passwd|pwd|token|secret|authorization)\s*[:=]\s*(?:'[^']*'|\"[^\"]*\"|\S+)",
                   r"\1=[redacted]", value, flags=re.I)
    value = re.split(r"\[SQL:|\[parameters:|\b(?:SELECT|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\b",
                     value, maxsplit=1, flags=re.I)[0]
    value = " ".join(value.split())[:500]
    return value or (type(error).__name__ if isinstance(error, BaseException) else "Execution requires review")


def failure_details(error):
    details = {"reason": type(error).__name__, "message": safe_failure_reason(error)}
    diagnostics = getattr(error, "diagnostics", {})
    results = diagnostics.get("results", []) if isinstance(diagnostics, dict) else []
    if isinstance(results, list):
        failures = [{"expiry": safe_failure_reason(row.get("expiry", "")),
                     "message": safe_failure_reason(row.get("error", row.get("error_type", "Fit rejected")))}
                    for row in results if isinstance(row, dict) and row.get("error")]
        if failures:
            details["failed_expiries"] = failures[:10]
    return details
