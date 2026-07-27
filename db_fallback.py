import logging
import re

import pandas as pd
from sqlalchemy import text

from runtime_config import config_value, get_database_engine


logger = logging.getLogger(__name__)

IDENTIFIER_PATTERN = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

DB_CONNECTION_STRING = config_value('DATABASE', 'CONNECTION_STRING')
DB_SCHEMA = config_value('DATABASE', 'SCHEMA', fallback='at_lng') or 'at_lng'
TRINOS_HOST = config_value('TRINOS', 'HOST')
TRINOS_USERNAME = config_value('TRINOS', 'USERNAME')
TRINOS_TOKEN = config_value('TRINOS', 'TOKEN')
TRINOS_PORT = config_value('TRINOS', 'PORT', fallback='443') or '443'


def quote_ident(identifier):
    if not IDENTIFIER_PATTERN.match(identifier):
        raise ValueError(f"Invalid SQL identifier: {identifier!r}")
    return f'"{identifier}"'


def fq_table(schema, table):
    return f"{quote_ident(schema)}.{quote_ident(table)}"


def sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def safe_exception_message(exc):
    message = str(exc)
    for sensitive_value in (TRINOS_TOKEN, DB_CONNECTION_STRING):
        if sensitive_value:
            message = message.replace(str(sensitive_value), "[redacted]")
    return message


def read_table_conn(conn, query):
    cursor = conn.cursor()
    try:
        cursor.execute(query)
        rows = cursor.fetchall()
        field_names = [field[0] for field in cursor.description] if cursor.description else []
        return pd.DataFrame(rows, columns=field_names)
    finally:
        cursor.close()


def read_trino_query(query, catalog='raw', schema='ice_gas'):
    try:
        from trino.dbapi import connect
        from trino.auth import JWTAuthentication
    except ImportError as exc:
        raise RuntimeError("trino package is not installed") from exc

    if not TRINOS_HOST or not TRINOS_USERNAME or not TRINOS_TOKEN:
        raise ValueError("TRINOS credentials are missing in config.ini")

    conn = connect(
        host=TRINOS_HOST,
        port=int(TRINOS_PORT) if TRINOS_PORT else 443,
        user=TRINOS_USERNAME,
        auth=JWTAuthentication(TRINOS_TOKEN),
        http_scheme="https",
        verify=False,
        catalog=catalog,
        schema=schema,
    )
    try:
        return read_table_conn(conn, query)
    finally:
        conn.close()


def read_with_fallback(
    trino_query,
    postgres_query,
    catalog='raw',
    schema='ice_gas',
    postgres_params=None,
    context_label='Data load',
):
    trino_message = None
    try:
        return read_trino_query(trino_query, catalog=catalog, schema=schema)
    except Exception as trino_exc:
        trino_message = safe_exception_message(trino_exc)
        message = f"{context_label} failed via Trino; using PostgreSQL fallback: {trino_message}"
        logger.warning(message)

    try:
        postgres_statement = text(postgres_query) if isinstance(postgres_query, str) else postgres_query
        return pd.read_sql(
            postgres_statement,
            con=get_database_engine(),
            params=postgres_params,
        )
    except Exception as postgres_exc:
        postgres_message = safe_exception_message(postgres_exc)
        raise RuntimeError(
            f"{context_label} failed via both Trino and PostgreSQL. "
            f"Trino error: {trino_message} | PostgreSQL error: {postgres_message}"
        ) from postgres_exc
