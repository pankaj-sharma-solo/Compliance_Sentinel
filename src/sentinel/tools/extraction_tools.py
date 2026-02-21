"""
Two-pass PDF extraction.
Pass 1 — cheap model: chunk by section header, extract candidate spans only.
Pass 2 — strong model: structured JSON output on candidate spans only.
Saves 60–70% tokens vs feeding full PDF to the strong model.
"""
import pdfplumber
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from sentinel.config import settings
import logging
import re

logger = logging.getLogger(__name__)

_cheap_llm = ChatOpenAI(model=settings.cheap_model, temperature=0.0, api_key=settings.openai_api_key)
_strong_llm = ChatOpenAI(model=settings.strong_model, temperature=0.0, api_key=settings.openai_api_key)

SECTION_HEADER_RE = re.compile(
    r"^(Article\s+\d+|Section\s+\d+|§\s*\d+|Chapter\s+\d+|\d+\.\d+)",
    re.IGNORECASE | re.MULTILINE,
)


def _chunk_pdf_by_section(pdf_path: str) -> list[dict]:
    """Extract text chunks keyed by section header."""
    chunks = []
    with pdfplumber.open(pdf_path) as pdf:
        current_section = "Preamble"
        buffer = []
        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            for line in text.split("\n"):
                match = SECTION_HEADER_RE.match(line.strip())
                if match:
                    if buffer:
                        chunks.append({
                            "section_header": current_section,
                            "text": "\n".join(buffer),
                            "page": page_num,
                        })
                    current_section = line.strip()
                    buffer = []
                else:
                    buffer.append(line)
        if buffer:
            chunks.append({"section_header": current_section, "text": "\n".join(buffer), "page": page_num})
    return chunks


@tool
def pass1_extract_candidates(pdf_path: str) -> list[dict]:
    """
    Pass 1 — cheap model.
    Chunk the PDF by section headers and identify which chunks
    contain candidate compliance rule spans.
    Returns: [{section_header, text, page, is_rule_candidate}]
    """
    chunks = _chunk_pdf_by_section(pdf_path)
    candidates = []
    for chunk in chunks:
        prompt = (
            "You are a compliance analyst. Does the following text contain a regulatory obligation, "
            "prohibition, or permission that could be decomposed into machine-checkable violation conditions? "
            "Reply with exactly 'YES' or 'NO'.\n\nTEXT:\n" + chunk["text"][:1500]
        )
        response = _cheap_llm.invoke(prompt)
        chunk["is_rule_candidate"] = response.content.strip().upper().startswith("Y")
        if chunk["is_rule_candidate"]:
            candidates.append(chunk)
    logger.info("Pass-1: %d/%d chunks are rule candidates", len(candidates), len(chunks))
    return candidates


@tool
def pass2_extract_structured_spans(candidates: list[dict], source_doc: str) -> list[dict]:
    """
    Pass 2 — strong model.
    For each candidate chunk, extract structured rule spans as JSON.
    Only runs on Pass-1 candidates — not the full PDF.
    Returns: [{span_text, article_ref, obligation_type, section_header, page, source_doc}]
    """
    spans = []
    for chunk in candidates:
        prompt = (
            "Extract all distinct compliance rules from this regulatory text. "
            "Return a JSON array where each item has: "
            "span_text (exact rule quote), article_ref (e.g. Article 44), "
            "obligation_type (PROHIBITION|REQUIREMENT|PERMISSION).\n\n"
            f"TEXT:\n{chunk['text'][:3000]}"
        )
        response = _strong_llm.invoke(prompt)
        try:
            import json
            extracted = json.loads(response.content)
            if isinstance(extracted, list):
                for item in extracted:
                    item["section_header"] = chunk["section_header"]
                    item["page"] = chunk["page"]
                    item["source_doc"] = source_doc
                    spans.append(item)
        except Exception as e:
            logger.warning("Pass-2 JSON parse failed for section %s: %s", chunk["section_header"], e)
    return spans
