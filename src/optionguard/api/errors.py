"""Error shape per DESIGN.md section 11: {error: {code, message, overridable?, login_url?}}."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from optionguard.market.base import NotAvailable
from optionguard.rules.errors import RuleViolation


class ApiError(Exception):
    def __init__(
        self, code: str, message: str, *, status_code: int = 400,
        overridable: bool | None = None, login_url: str | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.overridable = overridable
        self.login_url = login_url
        super().__init__(message)


def _body(code: str, message: str, overridable: bool | None = None, login_url: str | None = None) -> dict:
    err = {"code": code, "message": message}
    if overridable is not None:
        err["overridable"] = overridable
    if login_url is not None:
        err["login_url"] = login_url
    return {"error": err}


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=_body(exc.code, exc.message, exc.overridable, exc.login_url))


async def rule_violation_handler(request: Request, exc: RuleViolation) -> JSONResponse:
    return JSONResponse(status_code=409, content=_body(exc.code, exc.message, exc.overridable))


async def not_available_handler(request: Request, exc: NotAvailable) -> JSONResponse:
    return JSONResponse(status_code=409, content=_body("NOT_AVAILABLE", str(exc)))
