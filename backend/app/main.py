from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile, APIRouter
from fastapi.middleware.cors import CORSMiddleware

from .config import load_config
from .database import db, init_db, rows_to_dicts, pg_execute
from .invoice import extract_invoice_fields, extract_pdf_text
from .schemas import ChatRequest, ChatResponse, UploadResponse, UrlRequest, UrlResponse
from .services import answer_question, create_chat_if_needed, fetch_url_text, get_runtime_config, save_message

load_config()

app = FastAPI(title="TDSBot API", version="0.1.0")
api_router = APIRouter(prefix="/api")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Allow all for production Vercel
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    try:
        init_db()
    except Exception as e:
        print(f"CRITICAL: Startup initialization failed: {e}")


@api_router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@api_router.get("/config")
def config() -> dict[str, bool]:
    return get_runtime_config()


@api_router.post("/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest) -> ChatResponse:
    chat_id = create_chat_if_needed(payload.chat_id, payload.question)
    save_message(chat_id, "user", payload.question)
    answer, sources, confidence, matched_rule = await answer_question(payload.question, payload.document_ids)
    save_message(chat_id, "assistant", answer, sources)
    return ChatResponse(
        chat_id=chat_id,
        answer=answer,
        sources=sources,
        confidence=confidence,
        matched_rule=matched_rule,
    )


@api_router.post("/upload-pdf", response_model=UploadResponse)
async def upload_pdf(file: UploadFile = File(...)) -> UploadResponse:
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        text = extract_pdf_text(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    if not text.strip():
        raise HTTPException(status_code=422, detail="No extractable text found in the PDF.")
    invoice = extract_invoice_fields(text)
    with db() as conn:
        cur = pg_execute(conn, 
            """
            INSERT INTO documents (user_id, type, title, source_url, extracted_text)
            VALUES (?, ?, ?, ?, ?) RETURNING id
            """,
            (1, "pdf", file.filename, None, text),
        )
        document_id = int(cur.fetchone()["id"])
        pg_execute(conn, 
            """
            INSERT INTO invoice_extractions (
                document_id, vendor_name, invoice_number, invoice_date,
                amount, gst_details, party_details, items_json, extracted_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                invoice.get("vendor_name"),
                invoice.get("invoice_number"),
                invoice.get("invoice_date"),
                invoice.get("amount"),
                invoice.get("gst_details"),
                invoice.get("party_details"),
                invoice.get("items_json"),
                text,
            ),
        )
    invoice["document_id"] = document_id
    return UploadResponse(document_id=document_id, invoice=invoice, extracted_text_preview=text[:1200])


@api_router.post("/add-url", response_model=UrlResponse)
async def add_url(payload: UrlRequest) -> UrlResponse:
    try:
        fetched_title, text = await fetch_url_text(payload.url)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not fetch URL: {exc}") from exc
    title = payload.title or fetched_title
    with db() as conn:
        cur = pg_execute(conn, 
            """
            INSERT INTO documents (user_id, type, title, source_url, extracted_text)
            VALUES (?, ?, ?, ?, ?) RETURNING id
            """,
            (1, "url", title, payload.url, text),
        )
        document_id = int(cur.fetchone()["id"])
    return UrlResponse(document_id=document_id, title=title, extracted_text_preview=text[:1200])


@api_router.get("/chats")
def chats() -> list[dict]:
    with db() as conn:
        cur = pg_execute(conn, "SELECT * FROM chats ORDER BY created_at DESC")
        return rows_to_dicts(cur.fetchall())


@api_router.delete("/chats")
def clear_chats() -> dict[str, str]:
    with db() as conn:
        pg_execute(conn, "DELETE FROM messages")
        pg_execute(conn, "DELETE FROM chats")
    return {"status": "cleared"}


@api_router.get("/chats/{chat_id}")
def chat_detail(chat_id: int) -> dict:
    with db() as conn:
        cur = pg_execute(conn, "SELECT * FROM chats WHERE id = ?", (chat_id,))
        chat_row = cur.fetchone()
        if not chat_row:
            raise HTTPException(status_code=404, detail="Chat find not found.")
        cur_msg = pg_execute(conn, "SELECT * FROM messages WHERE chat_id = ? ORDER BY created_at ASC, id ASC", (chat_id,))
        messages = rows_to_dicts(cur_msg.fetchall())
    for message in messages:
        message["sources"] = json.loads(message.get("sources") or "[]")
    return {"chat": dict(chat_row), "messages": messages}

app.include_router(api_router)
