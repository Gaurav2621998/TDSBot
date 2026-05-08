from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Source(BaseModel):
    title: str
    url: str | None = None
    type: str = "reference"
    snippet: str | None = None


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1)
    chat_id: int | None = None
    document_ids: list[int] = Field(default_factory=list)


class ChatResponse(BaseModel):
    chat_id: int
    answer: str
    sources: list[Source]
    confidence: str
    matched_rule: dict[str, Any] | None = None


class UrlRequest(BaseModel):
    url: str
    title: str | None = None


class UrlResponse(BaseModel):
    document_id: int
    title: str
    extracted_text_preview: str


class UploadResponse(BaseModel):
    document_id: int
    invoice: dict[str, Any]
    extracted_text_preview: str
