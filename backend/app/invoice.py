from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pdfplumber


def extract_pdf_text(path: Path) -> str:
    pages: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text(x_tolerance=1, y_tolerance=3) or "")
    return "\n\n".join(page for page in pages if page.strip())


def first_match(patterns: list[str], text: str) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            return " ".join(match.group(1).strip().split())
    return None


def extract_invoice_fields(text: str) -> dict[str, Any]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    vendor_name = lines[0] if lines else None
    invoice_number = first_match(
        [
            r"invoice\s*(?:no|number|#)\s*[:\-]?\s*([A-Z0-9\/\-]+)",
            r"bill\s*(?:no|number|#)\s*[:\-]?\s*([A-Z0-9\/\-]+)",
        ],
        text,
    )
    invoice_date = first_match(
        [
            r"invoice\s*date\s*[:\-]?\s*([0-9]{1,2}[\/\-.][0-9]{1,2}[\/\-.][0-9]{2,4})",
            r"date\s*[:\-]?\s*([0-9]{1,2}[\/\-.][0-9]{1,2}[\/\-.][0-9]{2,4})",
            r"date\s*[:\-]?\s*([0-9]{1,2}\s+[A-Za-z]{3,9}\s+[0-9]{2,4})",
        ],
        text,
    )
    amount = first_match(
        [
            r"(?:grand\s+total|invoice\s+total|total\s+amount|amount\s+payable)\s*[:\-]?\s*(?:INR|Rs\.?|₹)?\s*([0-9,]+(?:\.[0-9]{1,2})?)",
            r"(?:INR|Rs\.?|₹)\s*([0-9,]+(?:\.[0-9]{1,2})?)",
        ],
        text,
    )
    gst_details = "; ".join(re.findall(r"(?:CGST|SGST|IGST|GSTIN|GST)\s*[:\-]?\s*[A-Z0-9.% ,]+", text, re.I)[:8])
    party_details = "; ".join(
        line for line in lines if re.search(r"\b(?:bill to|ship to|buyer|recipient|customer|gstin)\b", line, re.I)
    )[:1000]
    item_lines = [
        line
        for line in lines
        if re.search(
            r"\b(?:"
            r"service|services|goods|product|item|description|particulars|scope|work|fees?|charges?|"
            r"professional|consult|rent|lease|commission|brokerage|machine|software|support|maintenance|"
            r"repair|labou?r|contract|royalty|interest|equipment|materials?|supply|subscription"
            r")\b",
            line,
            re.I,
        )
    ][:12]
    return {
        "vendor_name": vendor_name,
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "amount": amount,
        "gst_details": gst_details or None,
        "party_details": party_details or None,
        "items_json": json.dumps(item_lines, ensure_ascii=False),
        "items": item_lines,
    }
