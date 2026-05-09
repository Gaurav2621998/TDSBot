from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from .config import load_config
from .database import supabase
from .invoice import extract_invoice_fields, extract_pdf_text
from .schemas import ChatRequest, ChatResponse, SupportRequest, UploadResponse, UrlRequest, UrlResponse
from .services import answer_question, create_chat_if_needed, fetch_url_text, get_runtime_config, save_message

load_config()

app = FastAPI(title="TDSBot API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    pass


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/config")
def config() -> dict[str, bool]:
    return get_runtime_config()


@app.post("/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest) -> ChatResponse:
    chat_id = create_chat_if_needed(payload.chat_id, payload.question)
    save_message(chat_id, "user", payload.question)
    answer, sources, confidence, matched_rule, support_eligible = await answer_question(payload.question, payload.document_ids, chat_id)
    save_message(chat_id, "assistant", answer, sources, support_eligible)
    return ChatResponse(
        chat_id=chat_id,
        answer=answer,
        sources=sources,
        confidence=confidence,
        matched_rule=matched_rule,
        support_eligible=support_eligible,
    )


@app.post("/support")
async def submit_support(payload: SupportRequest) -> dict[str, str]:
    supabase.table("support_queries").insert({
        "chat_id": payload.chat_id,
        "question": payload.question,
        "status": "pending"
    }).execute()
    return {"status": "success", "message": "Your query has been submitted for expert review."}


@app.post("/upload-pdf", response_model=UploadResponse)
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
    doc_res = supabase.table("documents").insert({
        "user_id": 101,
        "type": "pdf",
        "title": file.filename,
        "source_url": None,
        "extracted_text": text
    }).execute()
    document_id = doc_res.data[0]["id"]
    
    supabase.table("invoice_extractions").insert({
        "document_id": document_id,
        "vendor_name": invoice.get("vendor_name"),
        "invoice_number": invoice.get("invoice_number"),
        "invoice_date": invoice.get("invoice_date"),
        "amount": invoice.get("amount"),
        "gst_details": invoice.get("gst_details"),
        "party_details": invoice.get("party_details"),
        "items_json": invoice.get("items_json"),
        "extracted_text": text
    }).execute()
    
    invoice["document_id"] = document_id
    return UploadResponse(document_id=document_id, invoice=invoice, extracted_text_preview=text[:1200])


@app.post("/add-url", response_model=UrlResponse)
async def add_url(payload: UrlRequest) -> UrlResponse:
    try:
        fetched_title, text = await fetch_url_text(payload.url)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not fetch URL: {exc}") from exc
    title = payload.title or fetched_title
    doc_res = supabase.table("documents").insert({
        "user_id": 101,
        "type": "url",
        "title": title,
        "source_url": payload.url,
        "extracted_text": text
    }).execute()
    document_id = doc_res.data[0]["id"]
    return UrlResponse(document_id=document_id, title=title, extracted_text_preview=text[:1200])


@app.get("/chats")
def chats() -> list[dict]:
    res = supabase.table("chats").select("*").order("created_at", desc=True).execute()
    return res.data


@app.delete("/chats")
def clear_chats() -> dict[str, str]:
    # Delete all messages and chats for simplicity. Supabase REST doesn't support TRUNCATE easily without RPC.
    supabase.table("messages").delete().neq("id", 0).execute()
    supabase.table("chats").delete().neq("id", 0).execute()
    return {"status": "cleared"}


@app.get("/chats/{chat_id}")
def chat_detail(chat_id: int) -> dict:
    chat_res = supabase.table("chats").select("*").eq("id", chat_id).execute()
    if not chat_res.data:
        raise HTTPException(status_code=404, detail="Chat not found.")
    
    msg_res = supabase.table("messages").select("*").eq("chat_id", chat_id).order("created_at").order("id").execute()
    messages = msg_res.data
    
    for message in messages:
        message["sources"] = json.loads(message.get("sources") or "[]")
    return {"chat": chat_res.data[0], "messages": messages}
