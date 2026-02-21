"""
Two-pass PDF extraction.
Pass 1 — cheap model: chunk by section header, identify candidate spans only.
Pass 2 — strong model: with_structured_output on candidates only.
Saves 60–70% tokens vs feeding full PDF to the strong model.
"""
import re
import logging
import pdfplumber
from pydantic import BaseModel, Field
from typing import Literal
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool
from sentinel.config import settings

logger = logging.getLogger(__name__)

_cheap_llm = ChatGoogleGenerativeAI(
    model=settings.cheap_model,
    temperature=0.0,
    google_api_key=settings.google_api_key,
)
_strong_llm = ChatGoogleGenerativeAI(
    model=settings.strong_model,
    temperature=0.0,
    google_api_key=settings.google_api_key,
)

SECTION_HEADER_RE = re.compile(
    r"^(Article\s+\d+|Section\s+\d+|§\s*\d+|Chapter\s+\d+|\d+\.\d+)",
    re.IGNORECASE | re.MULTILINE,
)

# ── Structured output schema for Pass-2 ──────────────────────────────────────

class RuleSpan(BaseModel):
    """A single enforceable compliance rule extracted from regulatory text."""
    span_text: str = Field(
        description="The exact enforceable rule text — what a system MUST, SHOULD, or MUST NOT do"
    )
    article_ref: str = Field(
        description="Article or section reference e.g. 'Article 44', 'Section 3.1'"
    )
    obligation_type: Literal["REQUIREMENT", "PROHIBITION", "PERMISSION"] = Field(
        description="REQUIREMENT=must do, PROHIBITION=must not do, PERMISSION=may do"
    )

class ExtractedSpans(BaseModel):
    """All enforceable rule spans extracted from a regulatory text section."""
    spans: list[RuleSpan] = Field(
        description=(
            "List of enforceable rule spans. "
            "Leave empty if the section contains no enforceable obligations — "
            "e.g. audit logs, retention schedules, definitions, preamble."
        )
    )

# ── Pre-built structured LLM ──────────────────────────────────────────────────
_structured_llm = _strong_llm.with_structured_output(ExtractedSpans)

_PASS2_PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     "You are a compliance engineering expert. "
     "Extract ONLY enforceable rule spans from the given regulatory text. "
     "Enforceable rules describe what a system or organisation MUST, SHOULD, or MUST NOT do. "
     "IGNORE: audit log requirements, retention periods, false positive review processes, "
     "remediation procedures, definitions, recitals, preamble, and scope statements. "
     "If a section has no enforceable obligations, return spans as an empty list."),
    ("human",
     "Source document: {source_doc}\n"
     "Section: {section_header}\n\n"
     "TEXT:\n{text}"),
])


# ── PDF chunking — pure Python, zero LLM cost ────────────────────────────────

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


# ── Pass 1 — cheap model ──────────────────────────────────────────────────────

@tool
def pass1_extract_candidates(pdf_path: str) -> list[dict]:
    """
    Pass 1 — cheap model.
    Chunk the PDF by section headers and identify which chunks
    contain candidate compliance rule spans.
    Skips audit, retention, definitions, preamble sections automatically.
    Returns: [{section_header, text, page, is_rule_candidate}]
    """
    chunks = _chunk_pdf_by_section(pdf_path)
    candidates = []

    for chunk in chunks:
        # Pre-filter obvious non-rule sections without LLM call
        header_lower = chunk["section_header"].lower()
        skip_keywords = ["audit", "retention", "definition", "preamble", "scope",
                         "false positive", "remediation action", "glossary", "introduction"]
        if any(kw in header_lower for kw in skip_keywords):
            logger.debug("Pass-1: skipping non-rule section '%s'", chunk["section_header"])
            continue

        prompt = (
            "You are a compliance analyst. Does the following text contain a regulatory obligation, "
            "prohibition, or permission that could be decomposed into machine-checkable violation conditions? "
            "Reply with exactly 'YES' or 'NO'. Do not explain.\n\n"
            "TEXT:\n" + chunk["text"][:1500]
        )
        response = _cheap_llm.invoke(prompt)
        is_candidate = response.content.strip().upper().startswith("Y")
        chunk["is_rule_candidate"] = is_candidate
        if is_candidate:
            candidates.append(chunk)

    logger.info("Pass-1: %d/%d chunks are rule candidates", len(candidates), len(chunks))
    return candidates


# ── Pass 2 — strong model with structured output ──────────────────────────────

@tool
def pass2_extract_structured_spans(candidates: list[dict], source_doc: str) -> list[dict]:
    """
    Pass 2 — strong model with structured output.
    Extracts enforceable rule spans from Pass-1 candidates only.
    Uses with_structured_output — no JSON parsing, no parse failures.
    Returns: [{span_text, article_ref, obligation_type, section_header, page, source_doc}]
    """
    spans = []

    for chunk in candidates:
        try:
            result: ExtractedSpans = _structured_llm.invoke(
                _PASS2_PROMPT.format_messages(
                    source_doc=source_doc,
                    section_header=chunk["section_header"],
                    text=chunk["text"][:3000],
                )
            )

            if not result.spans:
                logger.debug(
                    "Pass-2: no enforceable rules in section '%s' — skipping",
                    chunk["section_header"]
                )
                continue

            for span in result.spans:
                spans.append({
                    **span.model_dump(),
                    "section_header": chunk["section_header"],
                    "page": chunk["page"],
                    "source_doc": source_doc,
                })

            logger.info(
                "Pass-2: extracted %d rules from section '%s'",
                len(result.spans), chunk["section_header"]
            )

        except Exception as e:
            logger.error(
                "Pass-2 failed for section '%s': %s",
                chunk["section_header"], e
            )

    logger.info("Pass-2 total: %d rule spans extracted from %d candidates", len(spans), len(candidates))
    return spans
