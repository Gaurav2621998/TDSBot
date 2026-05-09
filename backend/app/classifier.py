from __future__ import annotations

import re
from collections import Counter
from typing import Any

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "purchase of goods": [
        "goods",
        "purchase",
        "purchace",
        "purchance",
        "buy",
        "buying",
        "bought",
        "washing machine",
        "tv",
        "television",
        "refrigerator",
        "fridge",
        "air conditioner",
        "ac ",
        "laptop",
        "mobile",
        "phone",
        "computer",
        "printer",
        "equipment",
        "materials",
        "inventory",
        "product",
        "supply of goods",
    ],
    "contractor payment": [
        "contract",
        "contractor",
        "labour",
        "labor",
        "work order",
        "fabrication",
        "maintenance",
        "repair",
        "construction",
        "transport",
    ],
    "professional fees": [
        "professional",
        "consulting",
        "consultancy",
        "legal",
        "lawyer",
        "ca ",
        "chartered accountant",
        "doctor",
        "architect",
        "audit",
    ],
    "technical services": [
        "technical",
        "software implementation",
        "support services",
        "managed service",
        "call centre",
        "engineering service",
        "fts",
        "fees for technical",
    ],
    "rent": ["rent", "lease", "premises", "office space", "warehouse", "machinery rent"],
    "commission or brokerage": ["commission", "brokerage", "agent", "referral fee"],
    "interest": ["interest", "loan", "debenture", "securities"],
    "salary": ["salary", "wages", "payroll", "employee"],
    "insurance commission": ["insurance commission", "insurance brokerage"],
    "e-commerce transaction": ["ecommerce", "e-commerce", "marketplace", "online platform"],
    "non-resident/foreign payment": ["non resident", "non-resident", "foreign", "overseas", "import service", "195", "foreign entity", "foreign company", "nri", "royalty to", "payment to nri", "payment abroad"],
    "TCS applicable sale category": ["tcs", "scrap", "motor vehicle", "liquor", "timber", "coal", "lignite", "iron ore"],
    "purchase of property": ["immovable property", "land", "building", "flat", "house", "plot"],
    "winnings": ["lottery", "crossword", "card game", "gambling", "betting", "horse race", "online game", "winnings"],
    "other payments": ["other payment", "miscellaneous", "general", "residual"],
}

PURCHASE_PATTERNS = [
    r"\b(?:purchase|purchasing|purchased|buy|buying|bought)\s+(?:of\s+)?[a-z0-9][a-z0-9 ,/&.-]{1,80}",
    r"\b(?:tds|tcs)\b.{0,40}\b(?:purchase|purchasing|purchased|buy|buying|bought)\b",
]

SERVICE_HINTS = [
    "service",
    "services",
    "professional",
    "consulting",
    "consultancy",
    "technical",
    "contract",
    "contractor",
    "rent",
    "lease",
    "commission",
    "brokerage",
    "interest",
    "salary",
    "royalty",
]


def normalize(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    replacements = {
        "purchance": "purchase",
        "purchace": "purchase",
        "purhcase": "purchase",
        "televsion": "television",
    }
    for typo, correction in replacements.items():
        normalized = re.sub(rf"\b{re.escape(typo)}\b", correction, normalized)
    return normalized


def infer_purchase_of_goods(text: str) -> tuple[bool, list[str]]:
    haystack = normalize(text)
    if not any(re.search(pattern, haystack) for pattern in PURCHASE_PATTERNS):
        return False, []
    service_hits = [hint for hint in SERVICE_HINTS if re.search(rf"\b{re.escape(hint)}\b", haystack)]
    if service_hits:
        return False, service_hits
    return True, ["purchase/buy intent", "tangible goods unless described as service/rent/commission/etc."]


def classify_transaction(text: str) -> tuple[str | None, float, list[str]]:
    haystack = normalize(text)
    scores: Counter[str] = Counter()
    hits: dict[str, list[str]] = {}
    for category, keywords in CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            if keyword in haystack:
                scores[category] += 3 if " " in keyword else 1
                hits.setdefault(category, []).append(keyword)
    is_goods_purchase, purchase_hits = infer_purchase_of_goods(haystack)
    if is_goods_purchase:
        scores["purchase of goods"] += 6
        hits.setdefault("purchase of goods", []).extend(purchase_hits)
    if not scores:
        return None, 0.0, []
    category, score = scores.most_common(1)[0]
    confidence = min(0.95, 0.45 + score / 12)
    return category, confidence, hits.get(category, [])


def rank_rules(
    question: str, 
    rules: list[dict[str, Any]], 
    document_text: str = "", 
    pre_category: str | None = None,
    pre_confidence: float = 0.0
) -> list[tuple[dict[str, Any], float]]:
    combined = f"{question}\n{document_text[:5000]}"  # Limit doc text for ranking overlap
    category, category_confidence, hits = classify_transaction(question) # Classify only on QUESTION
    
    # Use pre-calculated classification if provided (e.g. from LLM)
    if pre_category:
        category = pre_category
        category_confidence = pre_confidence

    query_terms = set(re.findall(r"[a-z0-9]+", normalize(combined)))
    ranked: list[tuple[dict[str, Any], float]] = []
    for rule in rules:
        searchable = normalize(
            " ".join(
                [
                    rule["category"],
                    rule["nature_of_payment"],
                    rule["old_section"],
                    rule["new_section"],
                    rule["return_code"],
                    rule.get("notes", ""),
                ]
            )
        )
        terms = set(re.findall(r"[a-z0-9]+", searchable))
        overlap = len(query_terms & terms) / max(1, len(query_terms))
        score = overlap
        if category and rule["category"] == category:
            score += category_confidence
        if any(hit in searchable for hit in hits):
            score += 0.2
        ranked.append((rule, score))
    return sorted(ranked, key=lambda item: item[1], reverse=True)
