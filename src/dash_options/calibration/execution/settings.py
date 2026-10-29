"""Portable execution budgets; no database endpoints or transport tuning."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PublicationSettings:
    lock_timeout_ms: int = 5000
    statement_timeout_ms: int = 120000
    idle_transaction_timeout_ms: int = 60000

    @classmethod
    def from_environment(cls):
        values = {}
        for name, field in cls.__dataclass_fields__.items():
            key = "VOL_DAILY_" + name.upper()
            try:
                value = int(os.environ.get(key, field.default))
            except ValueError as exc:
                raise ValueError(f"{key} must be integer milliseconds") from exc
            if not 1 <= value <= 900000:
                raise ValueError(f"{key} must be between 1 and 900000 milliseconds")
            values[name] = value
        return cls(**values)

    def arguments(self):
        return dict(vars(self))
