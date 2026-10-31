"""Private, uncached authorization called for every transfer acceptance."""

import secrets
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import Field

from arclight.api.dependencies import Database
from arclight.api.schemas import Input
from arclight.application.file_transfer_grants import authorize_transfer
from arclight.core.errors import DomainError
from arclight.storage.transfer_protocol import TransferGrant

router = APIRouter(prefix="/internal/file-transfers", include_in_schema=False)


class TransferAuthorization(Input):
    ticket: str = Field(min_length=1, max_length=4096)
    method: Literal["GET", "POST"]


@router.post("/authorize")
def authorize(
    payload: TransferAuthorization, request: Request, response: Response, db: Database
) -> TransferGrant:
    settings = request.app.state.settings
    if not secrets.compare_digest(
        request.headers.get("X-File-Gateway-Key", ""),
        settings.file_gateway_secret.get_secret_value(),
    ):
        raise DomainError("UNAUTHORIZED", "文件网关认证失败", 401)
    response.headers["Cache-Control"] = "no-store"
    return authorize_transfer(db, settings, payload.ticket, payload.method)
