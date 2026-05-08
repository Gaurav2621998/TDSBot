from __future__ import annotations

import json
import re
from typing import Any

import httpx
from bs4 import BeautifulSoup

from .classifier import classify_transaction, rank_rules
from .config import get_setting, has_real_setting
from .database import db, rows_to_dicts
from .invoice import extract_invoice_fields
from .schemas import Source

TRUSTED_DOMAINS = [
    "incometax.gov.in",
    "incometaxindia.gov.in",
    "tdsman.com",
    "taxmann.com",
    "cleartax.in",
]

ALLOWED_CATEGORIES = [
    "purchase of goods",
    "contractor payment",
    "professional fees",
    "technical services",
    "rent",
    "commission or brokerage",
    "interest",
    "salary",
    "royalty",
    "insurance commission",
    "e-commerce transaction",
    "non-resident/foreign payment",
    "TCS applicable sale category",
]


def get_runtime_config() -> dict[str, bool]:
    return {
        "groq_enabled": has_real_setting("GROQ_API_KEY"),
        "serpapi_enabled": has_real_setting("SERPAPI_API_KEY"),
        "google_custom_search_enabled": has_real_setting("GOOGLE_API_KEY") and has_real_setting("GOOGLE_CSE_ID"),
    }


def get_rules() -> list[dict[str, Any]]:
    with db() as conn:
        return rows_to_dicts(conn.execute("SELECT * FROM tds_rules").fetchall())


def get_documents(document_ids: list[int]) -> list[dict[str, Any]]:
    if not document_ids:
        return []
    placeholders = ",".join("?" for _ in document_ids)
    with db() as conn:
        return rows_to_dicts(
            conn.execute(f"SELECT * FROM documents WHERE id IN ({placeholders})", document_ids).fetchall()
        )


def create_chat_if_needed(chat_id: int | None, question: str) -> int:
    if chat_id:
        return chat_id
    title = make_chat_title(question)
    with db() as conn:
        cur = conn.execute("INSERT INTO chats (user_id, title) VALUES (?, ?)", (1, title))
        return int(cur.lastrowid)


def make_chat_title(question: str) -> str:
    cleaned = re.sub(r"\s+", " ", question).strip()
    if not cleaned:
        return "New TDS chat"
    direct_lookup = re.search(r"\b(?:19[0-9][A-Z]?|20[0-9][A-Z]?|[0-9]{4})\b", cleaned, re.I)
    if direct_lookup:
        return f"Section {direct_lookup.group(0).upper()} lookup"
    lowered = cleaned.lower()
    if any(word in lowered for word in ["invoice", "agreement", "vendor", "bill"]):
        return "Invoice TDS analysis"
    if any(word in lowered for word in ["tds", "tcs", "section", "rate", "slab"]):
        title = re.sub(r"\b(?:what|which|how much|is|are|the|for|on|under|applicable|tds|tcs|section|rate|slab)\b", "", lowered, flags=re.I)
        title = re.sub(r"\s+", " ", title).strip(" ?.")
        if title:
            return title[:1].upper() + title[1:52]
    return cleaned[:52]


def save_message(chat_id: int, role: str, content: str, sources: list[Source] | None = None) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO messages (chat_id, role, content, sources) VALUES (?, ?, ?, ?)",
            (chat_id, role, content, json.dumps([source.model_dump() for source in sources or []])),
        )


async def fetch_url_text(url: str) -> tuple[str, str]:
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        response = await client.get(url, headers={"User-Agent": "TDSBot/1.0"})
        response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if "pdf" in content_type:
        return url.rsplit("/", 1)[-1] or "PDF reference", response.text
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else url
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
    return title, text[:120000]


def trusted_search_query(question: str) -> str:
    trusted_filter = " OR ".join(f"site:{domain}" for domain in TRUSTED_DOMAINS)
    return f"{question} TDS TCS FY 2026-27 ({trusted_filter})"


async def google_custom_search(question: str) -> list[Source]:
    api_key = get_setting("GOOGLE_API_KEY")
    cse_id = get_setting("GOOGLE_CSE_ID")
    if not api_key or not cse_id:
        return []
    params = {
        "key": api_key,
        "cx": cse_id,
        "q": trusted_search_query(question),
        "num": 5,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get("https://www.googleapis.com/customsearch/v1", params=params)
        response.raise_for_status()
    sources: list[Source] = []
    for result in response.json().get("items", [])[:5]:
        link = result.get("link")
        if not link:
            continue
        sources.append(
            Source(
                title=result.get("title") or link,
                url=link,
                type="web_search",
                snippet=result.get("snippet"),
            )
        )
    return sources


async def serpapi_search(question: str) -> list[Source]:
    api_key = get_setting("SERPAPI_API_KEY")
    if not api_key:
        return []
    query = trusted_search_query(question)
    params = {"engine": "google", "q": query, "api_key": api_key, "num": 5}
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get("https://serpapi.com/search.json", params=params)
        response.raise_for_status()
    data = response.json()
    sources: list[Source] = []
    for result in data.get("organic_results", [])[:5]:
        link = result.get("link")
        if not link:
            continue
        sources.append(
            Source(
                title=result.get("title") or link,
                url=link,
                type="web_search",
                snippet=result.get("snippet"),
            )
        )
    return sources


async def web_search(question: str) -> tuple[list[Source], str | None]:
    config = get_runtime_config()
    if config["google_custom_search_enabled"]:
        try:
            return await google_custom_search(question), None
        except Exception as exc:
            if not config["serpapi_enabled"]:
                return [], f"Google Custom Search failed: {exc}"

    if config["serpapi_enabled"]:
        try:
            return await serpapi_search(question), None
        except Exception as exc:
            return [], f"SerpAPI search failed: {exc}"

    return [], "Web search is not configured. Set GOOGLE_API_KEY and GOOGLE_CSE_ID, or set SERPAPI_API_KEY."


async def groq_refine(question: str, draft: str, context: str) -> str:
    api_key = get_setting("GROQ_API_KEY")
    if not api_key:
        return draft
    model = get_setting("GROQ_MODEL", "llama-3.3-70b-versatile")
    prompt = f"""
You are a cautious Indian TDS/TCS assistant. Rewrite the draft answer for clarity only.
Return only the final answer. Do not add a preamble such as "Rewritten answer" or commentary about rewriting.
Keep the same level of detail as the draft. If the draft is concise, keep it concise and professional.
If the draft has detailed headings, preserve the headings and do not remove required details.
Do not add tax rates, sections, thresholds, or codes unless they are present in the supplied draft/context.
If the source support is weak, preserve the uncertainty.

Question:
{question}

Source context:
{context[:6000]}

Draft answer:
{draft}
"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You produce source-grounded Indian TDS/TCS answers and never invent rates."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
    }
    async with httpx.AsyncClient(timeout=35) as client:
        response = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
        response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()


def wants_detailed_answer(question: str) -> bool:
    normalized = question.lower()
    detail_terms = [
        "detail",
        "detailed",
        "brief",
        "explain",
        "explanation",
        "reason",
        "reasoning",
        "why",
        "condition",
        "conditions",
        "exception",
        "threshold",
        "slab",
        "all details",
        "full",
    ]
    return any(term in normalized for term in detail_terms)


async def groq_classify_transaction(question: str, document_text: str) -> tuple[str | None, str | None]:
    api_key = get_setting("GROQ_API_KEY")
    if not api_key:
        return None, None
    model = get_setting("GROQ_MODEL", "llama-3.3-70b-versatile")
    prompt = f"""
Classify the user's Indian TDS/TCS question into exactly one category from this list:
{", ".join(ALLOWED_CATEGORIES)}

Rules:
- Return only JSON with keys category and reasoning.
- Use "purchase of goods" for questions about buying/purchasing tangible items unless the item is clearly a service, rent, commission, interest, salary, royalty, foreign payment, or TCS sale category.
- Do not provide tax rates, sections, return codes, or thresholds.
- If unclear, return null for category.

Question:
{question}

Uploaded invoice/reference text, if any:
{document_text[:3000]}
"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You only classify TDS/TCS transaction categories as JSON."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
    except Exception:
        return None, None
    category = data.get("category")
    if category not in ALLOWED_CATEGORIES:
        return None, data.get("reasoning")
    return category, data.get("reasoning")


def document_fact_summary(documents: list[dict[str, Any]]) -> tuple[str, list[str]]:
    if not documents:
        return "", []
    summaries: list[str] = []
    evidence: list[str] = []
    for doc in documents:
        text = doc["extracted_text"]
        fields = extract_invoice_fields(text)
        facts = [
            f"Document: {doc['title']} ({doc['type']})",
            f"Vendor/first party: {fields.get('vendor_name') or 'not found'}",
            f"Invoice/agreement number: {fields.get('invoice_number') or 'not found'}",
            f"Date: {fields.get('invoice_date') or 'not found'}",
            f"Amount: {fields.get('amount') or 'not found'}",
        ]
        items = fields.get("items") or []
        if items:
            facts.append("Likely line items/descriptions: " + " | ".join(items[:5]))
            evidence.extend(items[:5])
        else:
            short_lines = [line.strip() for line in text.splitlines() if 8 <= len(line.strip()) <= 180]
            evidence.extend(short_lines[:5])
            facts.append("Likely line items/descriptions: " + " | ".join(short_lines[:5]))
        summaries.append("\n".join(facts))
    return "\n\n".join(summaries), evidence[:8]


async def groq_analyze_transaction(
    question: str,
    document_text: str,
    document_summary: str,
) -> dict[str, Any] | None:
    api_key = get_setting("GROQ_API_KEY")
    if not api_key:
        return None
    model = get_setting("GROQ_MODEL", "llama-3.3-70b-versatile")
    prompt = f"""
Analyze the user's Indian TDS/TCS question and any uploaded invoice/agreement text.

Return only JSON with these keys:
- category: one of {ALLOWED_CATEGORIES}, or null if not enough information
- transaction_summary: short plain-English summary of what is being paid/sold
- question_intent: what the user wants, such as rate, slab, section, threshold, applicability, classification, or invoice analysis
- evidence: array of short phrases from the question/document that support the category
- reasoning: short reasoning for the category
- confidence: high, medium, or low

Rules:
- Do not provide tax rates, sections, return codes, or thresholds.
- If the document says consulting/professional/legal/audit/accounting, prefer professional fees.
- If it says technical support/software implementation/managed service/call centre/FTS, prefer technical services.
- If it says civil work/repair/maintenance/labour/work order/contractor, prefer contractor payment.
- If it says rent/lease/premises/equipment hire, prefer rent.
- If it says commission/brokerage/referral/agent, prefer commission or brokerage.
- If it is a tangible item/goods/product/material/equipment inventory purchase, prefer purchase of goods.
- If the question asks for a section/rate/slab but gives no transaction facts, return null category and explain what fact is missing.

Question:
{question}

Extracted document facts:
{document_summary[:2500]}

Raw uploaded/reference text:
{document_text[:5000]}
"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You classify Indian TDS/TCS transactions from questions and documents as JSON only."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
        data = json.loads(response.json()["choices"][0]["message"]["content"])
    except Exception:
        return None
    if data.get("category") not in ALLOWED_CATEGORIES:
        data["category"] = None
    return data


def choose_rule_for_category(category: str, rules: list[dict[str, Any]], context: str) -> dict[str, Any] | None:
    candidates = [rule for rule in rules if rule["category"] == category]
    if not candidates:
        return None
    normalized = context.lower()
    if category == "technical services":
        technical_words = ["technical", "software implementation", "managed service", "support", "call centre", "fts"]
        if any(word in normalized for word in technical_words):
            return candidates[0]
    if category == "professional fees":
        if "director" in normalized:
            director_rule = next((rule for rule in candidates if "Director" in rule["nature_of_payment"]), None)
            if director_rule:
                return director_rule
        professional_words = ["consulting", "consultancy", "legal", "audit", "accounting", "architect", "doctor", "professional"]
        if any(word in normalized for word in professional_words):
            return candidates[0]
    if category == "rent":
        if any(word in normalized for word in ["machinery", "plant", "equipment"]):
            return next((rule for rule in candidates if "machinery" in rule["nature_of_payment"].lower()), candidates[0])
        return next((rule for rule in candidates if "other than machinery" in rule["nature_of_payment"].lower()), candidates[0])
    if category == "interest":
        if "senior citizen" in normalized:
            return next((rule for rule in candidates if "senior citizen" in rule["nature_of_payment"].lower()), candidates[0])
        if "securities" in normalized:
            return next((rule for rule in candidates if "securities" in rule["nature_of_payment"].lower()), candidates[0])
        return next((rule for rule in candidates if "non-senior" in rule["nature_of_payment"].lower()), candidates[0])
    return candidates[0]


def direct_rule_matches(question: str, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = question.lower()
    matches: list[dict[str, Any]] = []
    for rule in rules:
        old_section = rule["old_section"].lower()
        return_code = rule["return_code"].lower()
        new_section = rule["new_section"].lower()
        old_base = re.sub(r"\(.*?\)", "", old_section).strip()
        if re.search(rf"\b{re.escape(return_code)}\b", normalized):
            matches.append(rule)
            continue
        if re.search(rf"\b{re.escape(old_section)}\b", normalized) or re.search(rf"\b{re.escape(old_base)}\b", normalized):
            matches.append(rule)
            continue
        compact_new = re.sub(r"\s+", "", new_section)
        compact_question = re.sub(r"\s+", "", normalized)
        if compact_new and compact_new in compact_question:
            matches.append(rule)
    seen: set[int] = set()
    unique: list[dict[str, Any]] = []
    for rule in matches:
        if rule["id"] not in seen:
            unique.append(rule)
            seen.add(rule["id"])
    return unique


def build_direct_rule_answer(question: str, matches: list[dict[str, Any]], sources: list[Source], detailed: bool) -> str:
    source_lines = "\n".join(f"- {source.title}: {source.url or 'uploaded document'}" for source in sources)
    rows = "\n".join(
        (
            f"- {rule['nature_of_payment']}: old Section {rule['old_section']}; "
            f"new Section {rule['new_section']}; return code {rule['return_code']}; "
            f"rate {rule['rate']}; threshold {rule['threshold']}; notes: {rule.get('notes') or 'Apply statutory conditions.'}"
        )
        for rule in matches
    )
    classification = matches[0]["category"] if len({rule["category"] for rule in matches}) == 1 else "Multiple rules under the requested section/code"
    if not detailed and len(matches) == 1:
        rule = matches[0]
        return f"""{rule['nature_of_payment']} is covered under old Section {rule['old_section']} and new Section {rule['new_section']}. The return code is {rule['return_code']}, the TDS/TCS rate is {rule['rate']}, and the threshold is {rule['threshold']}. {rule.get('notes') or 'Apply statutory conditions and exceptions.'}

Source: {sources[0].title} ({sources[0].url})

This is informational guidance only; please verify before deduction or filing."""
    if not detailed:
        compact_rows = "\n".join(
            f"- {rule['nature_of_payment']}: Section {rule['old_section']}, code {rule['return_code']}, rate {rule['rate']}, threshold {rule['threshold']}"
            for rule in matches
        )
        return f"""I found {len(matches)} possible rows for this section/code. Please choose the row matching the actual nature of payment:

{compact_rows}

Source: {sources[0].title} ({sources[0].url})

This is informational guidance only; please verify before deduction or filing."""
    return f"""Short answer: I found {len(matches)} matching TDS/TCS rule{'s' if len(matches) != 1 else ''} for your section/code query. Where a section has multiple rates, use the row matching the actual nature of payment.

Transaction classification: {classification}
Applicable old section: {", ".join(sorted({rule["old_section"] for rule in matches}))}
Applicable new section: {", ".join(sorted({rule["new_section"] for rule in matches}))}
Return code/section code: {", ".join(sorted({rule["return_code"] for rule in matches}))}
TDS/TCS rate: {", ".join(sorted({rule["rate"] for rule in matches}))}
Threshold: {", ".join(sorted({rule["threshold"] for rule in matches}))}
Conditions/exceptions: Match the nature of payment to the correct row. Details:
{rows}
Reasoning: I treated your question as a direct section/code lookup and returned the matching structured FY 2026-27 rule rows instead of forcing a single transaction classification.
Sources:
{source_lines}

Disclaimer: This is informational guidance only. Verify the facts, thresholds, PAN status, residency, exceptions and current law with a tax professional before filing or deduction."""


def build_answer(
    question: str,
    matched_rule: dict[str, Any] | None,
    confidence: str,
    classification: str | None,
    source_note: str,
    sources: list[Source],
    transaction_summary: str | None = None,
    document_summary: str | None = None,
    detailed: bool = False,
) -> str:
    disclaimer = (
        "Disclaimer: This is informational guidance only. Verify the facts, thresholds, PAN status, residency, "
        "exceptions and current law with a tax professional before filing or deduction."
    )
    if not matched_rule:
        source_lines = "\n".join(f"- {source.title}: {source.url or 'uploaded document'}" for source in sources) or "- No source found"
        if not detailed:
            context_sentence = f" I understood the transaction as {transaction_summary}." if transaction_summary else ""
            return f"""I could not identify a confident TDS/TCS rule from the available references.{context_sentence} Please provide the nature of payment, vendor/service description, amount, and whether the payee is resident/non-resident so I can classify it reliably.

Source status: {source_note}

This is informational guidance only; please verify before deduction or filing."""
        return f"""Short answer: I could not identify a confident TDS/TCS rule from the available references.

Transaction classification: {classification or "Unclear"}
Applicable old section: Not determined
Applicable new section: Not determined
Return code/section code: Not determined
TDS/TCS rate: Not determined
Threshold: Not determined
Conditions/exceptions: Verification required because no confident source-backed match was found.
Reasoning: {source_note} {f"Transaction understood as: {transaction_summary}. " if transaction_summary else ""}{f"Extracted document facts: {document_summary}. " if document_summary else ""}The query did not match the structured FY 2026-27 rules strongly enough.
Sources:
{source_lines}

{disclaimer}"""

    source_lines = "\n".join(f"- {source.title}: {source.url or 'uploaded document'}" for source in sources)
    if not detailed:
        summary = transaction_summary or matched_rule["nature_of_payment"]
        return f"""{summary} is generally classified as {classification or matched_rule['category']}. The applicable old section is {matched_rule['old_section']} and the new section is {matched_rule['new_section']}. The return code is {matched_rule['return_code']}, the TDS/TCS rate is {matched_rule['rate']}, and the threshold is {matched_rule['threshold']}. {matched_rule.get('notes') or 'Apply statutory conditions and exceptions.'}

Source: TDSMAN TDS/TCS Rate Chart FY 2026-27 ({matched_rule['source_url']})

This is informational guidance only; please verify before deduction or filing."""
    return f"""Short answer: {matched_rule["nature_of_payment"]} is generally covered under old Section {matched_rule["old_section"]}, new Section {matched_rule["new_section"]}, return code {matched_rule["return_code"]}, at {matched_rule["rate"]}, subject to the threshold and conditions below.

Transaction classification: {classification or matched_rule["category"]}
Applicable old section: {matched_rule["old_section"]}
Applicable new section: {matched_rule["new_section"]}
Return code/section code: {matched_rule["return_code"]}
TDS/TCS rate: {matched_rule["rate"]}
Threshold: {matched_rule["threshold"]}
Conditions/exceptions: {matched_rule.get("notes") or "Apply statutory conditions and exceptions."}
Reasoning: {source_note} {f"Transaction understood as: {transaction_summary}. " if transaction_summary else ""}{f"Extracted document facts: {document_summary}. " if document_summary else ""}The payment description maps to "{matched_rule["category"]}", which matches the TDSMAN FY 2026-27 rule for "{matched_rule["nature_of_payment"]}". Confidence: {confidence}.
Sources:
{source_lines}

{disclaimer}"""


async def answer_question(question: str, document_ids: list[int]) -> tuple[str, list[Source], str, dict[str, Any] | None]:
    documents = get_documents(document_ids)
    document_text = "\n\n".join(doc["extracted_text"] for doc in documents)
    doc_summary, doc_evidence = document_fact_summary(documents)
    detailed = wants_detailed_answer(question)
    classification, cls_score, _ = classify_transaction(f"{question}\n{document_text}")
    rules = get_rules()
    direct_matches = direct_rule_matches(question, rules)
    if direct_matches and not document_text.strip():
        sources = [
            Source(
                title="TDSMAN TDS/TCS Rate Chart FY 2026-27",
                url=direct_matches[0]["source_url"],
                type="default_reference",
                snippet="Direct section/code lookup",
            )
        ]
        draft = build_direct_rule_answer(question, direct_matches, sources, detailed)
        return await groq_refine(question, draft, json.dumps(direct_matches, ensure_ascii=False)), sources, "high", direct_matches[0]
    ranked = rank_rules(question, rules, document_text)
    best_rule, score = ranked[0] if ranked else (None, 0)
    llm_reasoning = None
    transaction_summary = None
    analysis = None
    if get_runtime_config()["groq_enabled"]:
        analysis = await groq_analyze_transaction(question, document_text, doc_summary)
    if analysis and analysis.get("category"):
        classification = analysis["category"]
        llm_reasoning = analysis.get("reasoning")
        transaction_summary = analysis.get("transaction_summary")
        selected_rule = choose_rule_for_category(classification, rules, f"{question}\n{doc_summary}\n{document_text}")
        if selected_rule:
            best_rule = selected_rule
            score = 0.82 if analysis.get("confidence") == "high" else 0.62
    elif score < 0.35 and get_runtime_config()["groq_enabled"]:
        llm_category, llm_reasoning = await groq_classify_transaction(question, document_text)
        if llm_category:
            classification = llm_category
            selected_rule = choose_rule_for_category(llm_category, rules, f"{question}\n{doc_summary}\n{document_text}")
            if selected_rule:
                best_rule = selected_rule
                score = 0.62
    confidence = "high" if score >= 0.65 else "medium" if score >= 0.35 else "low"

    sources = [
        Source(title=doc["title"], url=doc["source_url"], type=doc["type"], snippet=doc["extracted_text"][:240])
        for doc in documents
    ]
    source_note = "I checked uploaded invoice/reference document content first." if documents else "I checked the default structured FY 2026-27 TDS/TCS rules first."
    if llm_reasoning and classification:
        source_note = f"{source_note} Groq classified the transaction as '{classification}' because {llm_reasoning}"
    if doc_evidence:
        source_note = f"{source_note} Document evidence considered: {' | '.join(doc_evidence[:4])}."

    if best_rule and score >= 0.35:
        sources.append(
            Source(
                title="TDSMAN TDS/TCS Rate Chart FY 2026-27",
                url=best_rule["source_url"],
                type="default_reference",
                snippet=f'{best_rule["old_section"]} | {best_rule["new_section"]} | {best_rule["rate"]}',
            )
        )
        draft = build_answer(
            question,
            best_rule,
            confidence,
            classification,
            source_note,
            sources,
            transaction_summary=transaction_summary,
            document_summary=doc_summary.replace("\n", "; ")[:1200] if doc_summary else None,
            detailed=detailed,
        )
        context = document_text + "\n" + json.dumps(best_rule, ensure_ascii=False)
        return await groq_refine(question, draft, context), sources, confidence, best_rule

    web_sources, search_error = await web_search(question)
    if web_sources:
        sources.extend(web_sources)
        source_note = "I could not find this in the uploaded/reference documents, so I checked online sources."
    elif search_error:
        source_note = f"{source_note} {search_error}"
    draft = build_answer(
        question,
        None,
        "low",
        classification,
        source_note,
        sources,
        transaction_summary=transaction_summary,
        document_summary=doc_summary.replace("\n", "; ")[:1200] if doc_summary else None,
        detailed=detailed or confidence == "low",
    )
    return await groq_refine(question, draft, "\n".join(source.snippet or "" for source in sources)), sources, "low", None
