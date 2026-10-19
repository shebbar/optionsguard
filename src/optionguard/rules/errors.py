from __future__ import annotations


class RuleViolation(Exception):
    def __init__(self, code: str, message: str, overridable: bool = False) -> None:
        self.code = code
        self.message = message
        self.overridable = overridable
        super().__init__(message)
