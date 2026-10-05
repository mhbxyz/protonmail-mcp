from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Folder(BaseModel):
    name: str
    delimiter: str = ""
    selectable: bool = True
    flags: list[str] = Field(default_factory=list)


class EmailSummary(BaseModel):
    message_id: str
    folder: str
    uid: int
    subject: str = ""
    sender: str = ""
    recipients: str = ""
    date: str = ""
    received: str = ""
    unread: bool = True
    flagged: bool = False
    size_bytes: int = 0


class Attachment(BaseModel):
    filename: str
    content_type: str
    size_bytes: int


class EmailContent(EmailSummary):
    cc: str = ""
    reply_to: str = ""
    body_text: str = ""
    truncated: bool = False
    attachments: list[Attachment] = Field(default_factory=list)


class DraftCreated(BaseModel):
    uid: int
    message_id: str
    folder: str
    subject: str = ""


class DraftDeleted(BaseModel):
    uid: int
    message_id: str


class PreparedAction(BaseModel):
    token: str
    action: str
    expires_in: float
    preview: dict[str, Any]
