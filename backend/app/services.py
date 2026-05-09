from __future__ import annotations

import json
import re
from typing import Any

import httpx
from bs4 import BeautifulSoup

from .classifier import classify_transaction, rank_rules
from .config import get_setting, has_real_setting
from .database import supabase
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
        "tavily_enabled": has_real_setting("TAVILY_API_KEY"),
        "serpapi_enabled": has_real_setting("SERPAPI_API_KEY"),
        "google_custom_search_enabled": has_real_setting("GOOGLE_API_KEY") and has_real_setting("GOOGLE_CSE_ID"),
    }


def get_rules() -> list[dict[str, Any]]:
    res = supabase.table("tds_rules").select("*").execute()
    return res.data


def get_documents(document_ids: list[int]) -> list[dict[str, Any]]:
    if not document_ids:
        return []
    res = supabase.table("documents").select("*").in_("id", document_ids).execute()
    return res.data


def get_default_references() -> list[dict[str, Any]]:
    """Always loads all default_reference documents (seeded expert knowledge) from Supabase."""
    res = supabase.table("documents").select("*").eq("type", "default_reference").execute()
    return res.data or []


def get_relevant_qa_pairs(question: str, top_n: int = 5) -> str:
    """Returns the top N most relevant QA pairs from the training dataset via keyword overlap."""
    import json as _json
    from pathlib import Path as _Path
    qa_path = _Path(__file__).parent / "data" / "tds_qa_training_dataset-(1).json"
    if not qa_path.exists():
        return ""
    try:
        data = _json.loads(qa_path.read_text(encoding="utf-8"))
        qa_pairs = data.get("qa_pairs", [])
    except Exception:
        return ""
    q_words = set(re.findall(r"\b\w{3,}\b", question.lower()))
    scored = []
    for pair in qa_pairs:
        text = (pair.get("question", "") + " " + pair.get("answer", "")).lower()
        overlap = len(q_words & set(re.findall(r"\b\w{3,}\b", text)))
        if overlap > 0:
            scored.append((overlap, pair))
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        return ""
    lines = []
    for _, pair in scored[:top_n]:
        lines.append(f"Q: {pair['question']}\nA: {pair['answer']}")
    return "\n\n".join(lines)

def create_chat_if_needed(chat_id: int | None, question: str) -> int:
    if chat_id:
        return chat_id
    title = make_chat_title(question)
    res = supabase.table("chats").insert({"user_id": 101, "title": title}).execute()
    return res.data[0]["id"]


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


def save_message(chat_id: int, role: str, content: str, sources: list[Source] | None = None, support_eligible: bool = False) -> None:
    supabase.table("messages").insert({
        "chat_id": chat_id,
        "role": role,
        "content": content,
        "sources": json.dumps([s.dict() for s in sources]) if sources else None,
        "support_eligible": support_eligible
    }).execute()


async def fetch_url_text(url: str) -> tuple[str, str]:
    try:
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
    except Exception as e:
        print(f"Error fetching URL {url}: {e}")
        return url, ""


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
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get("https://www.googleapis.com/customsearch/v1", params=params)
            response.raise_for_status()
    except Exception as e:
        print(f"Google Search failed: {e}")
        return []
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
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get("https://serpapi.com/search.json", params=params)
            response.raise_for_status()
        data = response.json()
    except Exception as e:
        print(f"SerpApi Search failed: {e}")
        return []
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


async def tavily_search(question: str) -> list[Source]:
    api_key = get_setting("TAVILY_API_KEY")
    if not api_key:
        return []
    query = trusted_search_query(question)
    payload = {
        "api_key": api_key,
        "query": query,
        "search_depth": "basic",
        "max_results": 5,
        "include_domains": TRUSTED_DOMAINS
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post("https://api.tavily.com/search", json=payload)
            response.raise_for_status()
        data = response.json()
    except Exception as e:
        print(f"Tavily Search failed: {e}")
        return []
    sources: list[Source] = []
    for result in data.get("results", [])[:5]:
        link = result.get("url")
        if not link:
            continue
        sources.append(
            Source(
                title=result.get("title") or link,
                url=link,
                type="web_search",
                snippet=result.get("content"),
            )
        )
    return sources


async def web_search(question: str) -> tuple[list[Source], str | None]:
    config = get_runtime_config()
    sources: list[Source] = []

    # Priority 1: Tavily (1000 searches/mo)
    if config["tavily_enabled"]:
        sources = await tavily_search(question)
        if sources:
            return sources, None

    # Priority 2: Google Custom Search
    if config["google_custom_search_enabled"]:
        sources = await google_custom_search(question)
        if sources:
            return sources, None

    # Priority 3: SerpApi
    if config["serpapi_enabled"]:
        sources = await serpapi_search(question)
        if sources:
            return sources, None

    return [], "No results found from web search."


async def groq_refine(question: str, draft: str, context: str, matching_rules: str = "", qa_examples: str = "") -> str:
    api_key = get_setting("GROQ_API_KEY")
    if not api_key:
        return draft
    model = get_setting("GROQ_MODEL", "llama-3.3-70b-versatile")

    system_prompt = """You are a senior Indian TDS/TCS tax expert under the Income Tax Act, 2025.

DEFAULT: Give CONCISE answers. Use this compact format for every response unless the user explicitly asks to explain or elaborate:

[One sentence describing the transaction]
Return code: XXXX | Old section: XXX | New section: XXX [Sl. X]
Rate: X% | Threshold: ₹X | Form: XXX | Deductee: Resident/Non-resident
[One line of notes if relevant]

MANDATORY RULES:
1. ALWAYS include the 4-digit return code (e.g. 1009).
2. ALWAYS include new section reference AND old section.
3. ALWAYS state rate, threshold, return form, deductee category.
4. For transactions on/after 1 April 2026 → NEW section + return code.
5. For transactions before 1 April 2026 → OLD section only.
6. NEVER invent codes or rates. Only use provided knowledge base.
7. If multiple codes apply, list each on its own line.
8. If user says 'explain', 'detail', 'why', 'elaborate' → expand with full reasoning."""

    if matching_rules:
        system_prompt += f"\n\nRELEVANT RETURN CODES FROM KNOWLEDGE BASE:\n{matching_rules}"
    if qa_examples:
        system_prompt += f"\n\nFEW-SHOT REFERENCE EXAMPLES (for accuracy):\n{qa_examples}"

    prompt = f"""Question: {question}

Source context:
{context[:4000]}

Draft answer (rewrite using mandatory rules above):
{draft}"""

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
    }
    try:
        async with httpx.AsyncClient(timeout=35) as client:
            response = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"Error calling Groq API: {e}")
        return draft


def wants_detailed_answer(question: str) -> bool:
    """Returns True only when user explicitly asks for a detailed/explained answer."""
    normalized = question.lower()
    explicit_detail_terms = [
        "detail",
        "detailed",
        "explain",
        "explanation",
        "why",
        "all details",
        "full details",
        "elaborate",
        "in detail",
        "break down",
        "walk me through",
    ]
    return any(term in normalized for term in explicit_detail_terms)


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
    if category == "non-resident/foreign payment":
        # Royalty, technical fees, general payments to NR → residual code 1057 (Section 195 replacement)
        if any(w in normalized for w in ["royalty", "technical fee", "professional", "interest on loan", "general"]):
            residual = next((r for r in candidates if r["return_code"] == "1057"), None)
            if residual:
                return residual
        # Interest on bonds / infrastructure debt → specific codes
        if "bond" in normalized or "infrastructure debt" in normalized:
            return next((r for r in candidates if r["return_code"] in ("1040", "1044")), candidates[0])
        # Default NR fallback: 1057 residual
        return next((r for r in candidates if r["return_code"] == "1057"), candidates[0])

    return candidates[0]


def direct_rule_matches(question: str, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = question.lower()
    matches: list[dict[str, Any]] = []

    # Form number lookup: "what is form 144" → return all rules that use Form 144
    form_match = re.search(r"\bform\s+(\d+)\b", normalized)
    if form_match:
        form_str = f"form {form_match.group(1)}"
        form_hits = [r for r in rules if form_str in (r.get("notes") or "").lower()]
        if form_hits:
            return form_hits[:10]

    # Foreign + royalty → force residual NR code 1057
    is_foreign = any(w in normalized for w in ["foreign", "non-resident", "non resident", "overseas", "nri", "abroad"])
    is_royalty_or_general = any(w in normalized for w in ["royalty", "payment abroad", "general nr"])
    if is_foreign and is_royalty_or_general:
        residual = next((r for r in rules if r["return_code"] == "1057"), None)
        if residual:
            matches.append(residual)

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
    source_lines = "\n".join(f"- {source.url or 'uploaded document'}" for source in sources)
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
        return f"""{rule['nature_of_payment']} is covered under old Section {rule['old_section']} and new Section {rule['new_section']}. The return code is {rule['return_code']}, the TDS/TCS rate is {rule['rate']}, and the threshold is {rule['threshold']}. {rule.get('notes') or 'Apply statutory conditions and exceptions.'}"""
    if not detailed:
        compact_rows = "\n".join(
            f"- {rule['nature_of_payment']}: Section {rule['old_section']}, code {rule['return_code']}, rate {rule['rate']}, threshold {rule['threshold']}"
            for rule in matches
        )
        return f"""I found {len(matches)} possible rows for this section/code. Please choose the row matching the actual nature of payment:

{compact_rows}"""
    return f"""Short answer: I found {len(matches)} matching TDS/TCS rule{'s' if len(matches) != 1 else ''} for your section/code query. Where a section has multiple rates, use the row matching the actual nature of payment.

Transaction classification: {classification}
Applicable old section: {", ".join(sorted({rule["old_section"] for rule in matches}))}
Applicable new section: {", ".join(sorted({rule["new_section"] for rule in matches}))}
Return code/section code: {", ".join(sorted({rule["return_code"] for rule in matches}))}
TDS/TCS rate: {", ".join(sorted({rule["rate"] for rule in matches}))}
Threshold: {", ".join(sorted({rule["threshold"] for rule in matches}))}
Conditions/exceptions: Match the nature of payment to the correct row. Details:
{rows}
Reasoning: I treated your question as a direct section/code lookup and returned the matching structured FY 2026-27 rule rows instead of forcing a single transaction classification."""


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
        source_lines = "\n".join(f"- {source.url or 'uploaded document'}" for source in sources) or "- No source found"
        if not detailed:
            context_sentence = f" I understood the transaction as {transaction_summary}." if transaction_summary else ""
            return f"""I could not identify a confident TDS/TCS rule from the available references.{context_sentence} Please provide the nature of payment, vendor/service description, amount, and whether the payee is resident/non-resident so I can classify it reliably.

Source status: {source_note}"""
        return f"""Short answer: I could not identify a confident TDS/TCS rule from the available references.

Transaction classification: {classification or "Unclear"}
Applicable old section: Not determined
Applicable new section: Not determined
Return code/section code: Not determined
TDS/TCS rate: Not determined
Threshold: Not determined
Conditions/exceptions: Verification required because no confident source-backed match was found.
Reasoning: {source_note} {f"Transaction understood as: {transaction_summary}. " if transaction_summary else ""}{f"Extracted document facts: {document_summary}. " if document_summary else ""}The query did not match the structured FY 2026-27 rules strongly enough."""

    source_lines = "\n".join(f"- {source.url or 'uploaded document'}" for source in sources)
    if not detailed:
        summary = transaction_summary or matched_rule["nature_of_payment"]
        return f"""{summary} is generally classified as {classification or matched_rule['category']}. The applicable old section is {matched_rule['old_section']} and the new section is {matched_rule['new_section']}. The return code is {matched_rule['return_code']}, the TDS/TCS rate is {matched_rule['rate']}, and the threshold is {matched_rule['threshold']}. {matched_rule.get('notes') or 'Apply statutory conditions and exceptions.'}"""
    return f"""Short answer: {matched_rule["nature_of_payment"]} is generally covered under old Section {matched_rule["old_section"]}, new Section {matched_rule["new_section"]}, return code {matched_rule["return_code"]}, at {matched_rule["rate"]}, subject to the threshold and conditions below.

Transaction classification: {classification or matched_rule["category"]}
Applicable old section: {matched_rule["old_section"]}
Applicable new section: {matched_rule["new_section"]}
Return code/section code: {matched_rule["return_code"]}
TDS/TCS rate: {matched_rule["rate"]}
Threshold: {matched_rule["threshold"]}
Conditions/exceptions: {matched_rule.get("notes") or "Apply statutory conditions and exceptions."}
Reasoning: {source_note} {f"Transaction understood as: {transaction_summary}. " if transaction_summary else ""}{f"Extracted document facts: {document_summary}. " if document_summary else ""}The payment description maps to "{matched_rule["category"]}", which matches the TDSMAN FY 2026-27 rule for "{matched_rule["nature_of_payment"]}". Confidence: {confidence}."""


async def answer_question(question: str, document_ids: list[int], chat_id: int | None = None) -> tuple[str, list[Source], str, dict[str, Any] | None, bool]:
    # Load user-selected docs + always-on default references
    user_docs = get_documents(document_ids)
    default_docs = get_default_references()
    # Default references come first; user-uploaded docs override/append
    all_default_ids = {d["id"] for d in default_docs}
    extra_user_docs = [d for d in user_docs if d["id"] not in all_default_ids]
    documents = default_docs + extra_user_docs

    # Separate invoice/user docs from reference docs for display purposes
    reference_docs_text = "\n\n".join(
        doc["extracted_text"] for doc in default_docs
    )
    user_doc_text = "\n\n".join(doc["extracted_text"] for doc in extra_user_docs)
    document_text = (user_doc_text + "\n\n" + reference_docs_text).strip()

    doc_summary, doc_evidence = document_fact_summary(extra_user_docs)  # summary only from user-uploaded

    chat_context = ""
    if chat_id:
        msg_res = supabase.table("messages").select("role, content").eq("chat_id", chat_id).order("created_at").execute()
        history = msg_res.data
        history_to_use = history[:-1][-6:] if len(history) > 1 else []
        if history_to_use:
            formatted_history = "\n".join([f"{m['role'].capitalize()}: {m['content']}" for m in history_to_use])
            chat_context = f"Previous conversation context:\n{formatted_history}\n\n"

    contextual_question = f"{chat_context}Current question: {question}" if chat_context else question
    detailed = wants_detailed_answer(contextual_question)

    # Get relevant QA pairs for few-shot prompting
    qa_examples = get_relevant_qa_pairs(contextual_question)

    classification, cls_score, _ = classify_transaction(f"{contextual_question}\n{user_doc_text}")
    rules = get_rules()

    # Build a formatted string of top matching rules for the LLM
    direct_matches = direct_rule_matches(contextual_question, rules)
    matching_rules_str = "\n".join(
        f"Code {r['return_code']} | {r['old_section']} → {r['new_section']} | {r['nature_of_payment']} | Rate: {r['rate']} | Threshold: {r['threshold']} | {r.get('notes', '')}"
        for r in direct_matches[:8]
    ) if direct_matches else ""

    if direct_matches and not user_doc_text.strip():
        sources = [
            Source(
                title="TDS/TCS Rate Chart FY 2026-27 + Act 2025",
                url=direct_matches[0]["source_url"],
                type="default_reference",
                snippet="Direct section/code lookup",
            )
        ]
        draft = build_direct_rule_answer(question, direct_matches, sources, detailed)
        return await groq_refine(contextual_question, draft, json.dumps(direct_matches, ensure_ascii=False), matching_rules_str, qa_examples), sources, "high", direct_matches[0], False
    ranked = rank_rules(contextual_question, rules, user_doc_text, classification, cls_score)
    best_rule, score = ranked[0] if ranked else (None, 0)
    llm_reasoning = None
    transaction_summary = None
    analysis = None
    if get_runtime_config()["groq_enabled"]:
        analysis = await groq_analyze_transaction(contextual_question, document_text, doc_summary)
    if analysis and analysis.get("category"):
        classification = analysis["category"]
        llm_reasoning = analysis.get("reasoning")
        transaction_summary = analysis.get("transaction_summary")
        selected_rule = choose_rule_for_category(classification, rules, f"{contextual_question}\n{doc_summary}\n{document_text}")
        if selected_rule:
            best_rule = selected_rule
            score = 0.82 if analysis.get("confidence") == "high" else 0.62
    elif score < 0.35 and get_runtime_config()["groq_enabled"]:
        llm_category, llm_reasoning = await groq_classify_transaction(contextual_question, document_text)
        if llm_category:
            classification = llm_category
            selected_rule = choose_rule_for_category(llm_category, rules, f"{contextual_question}\n{doc_summary}\n{document_text}")
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
                title="TDS/TCS Rate Chart FY 2026-27 + Act 2025",
                url=best_rule["source_url"],
                type="default_reference",
                snippet=f'{best_rule["return_code"]} | {best_rule["old_section"]} → {best_rule["new_section"]} | {best_rule["rate"]}',
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
        context = user_doc_text + "\n" + json.dumps(best_rule, ensure_ascii=False)
        return await groq_refine(contextual_question, draft, context, matching_rules_str, qa_examples), sources, confidence, best_rule, False

    web_sources, search_error = await web_search(contextual_question)
    if web_sources:
        sources.extend(web_sources)
        source_note = "I could not find this in the uploaded/reference documents, so I checked online sources."
    elif search_error:
        source_note = f"{source_note} {search_error}"
        
    support_eligible = not bool(web_sources)
    if support_eligible:
        source_note += " No direct match found. You can submit this query to our tax experts for manual review."

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
    final_answer = await groq_refine(contextual_question, draft, "\n".join(source.snippet or "" for source in sources), matching_rules_str, qa_examples)
    return final_answer, sources, "low", None, support_eligible
