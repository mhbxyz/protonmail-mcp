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
    references: str = ""
    body_text: str = ""
    body_markdown: str = ""
    links: list[str] = Field(default_factory=list)
    quoted_removed: bool = False
    content_trust: str = "untrusted"
    truncated: bool = False
    attachments: list[Attachment] = Field(default_factory=list)


class DraftCreated(BaseModel):
    uid: int
    message_id: str
    folder: str
    subject: str = ""
    duplicate: bool = False


class DraftPreview(BaseModel):
    headers: dict[str, str]
    recipients: list[str] = Field(default_factory=list)
    external_recipients: list[str] = Field(default_factory=list)
    body_text: str = ""
    raw: str = ""


class OrganizeResult(BaseModel):
    action: str
    folder: str = ""
    destination: str = ""
    updated: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class FolderStatus(BaseModel):
    name: str
    total: int = 0
    unread: int = 0


class EmailPage(BaseModel):
    folder: str
    messages: list[EmailSummary] = Field(default_factory=list)
    next_cursor: str | None = None


class Digest(BaseModel):
    folder: str
    since_days: int
    unread: int
    total_recent: int
    messages: list[EmailSummary] = Field(default_factory=list)


class SavedFile(BaseModel):
    filename: str
    path: str
    size_bytes: int
    content_type: str = ""


class DraftDeleted(BaseModel):
    uid: int
    message_id: str


class DraftSent(BaseModel):
    message_id: str
    recipients: list[str] = Field(default_factory=list)
    duplicate: bool = False
    warnings: list[str] = Field(default_factory=list)


class PreparedAction(BaseModel):
    token: str
    action: str
    expires_in: float
    preview: dict[str, Any]
