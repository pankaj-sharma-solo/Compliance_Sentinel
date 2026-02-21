"""
Rule decomposition — the critical cost boundary step.
LLM interprets regulatory language ONCE here.
All downstream scanning is programmatic using the output.

Uses include_raw=True for validation-feedback retry:
the LLM gets its own parse error fed back as context
rather than blind retrying.
"""
from pathlib import Path

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai.chat_models import ChatGoogleGenerativeAI
from sentinel.states.state import DecomposedRule, ViolationCondition
from sentinel.config import settings
import logging
import json
import hashlib


logger = logging.getLogger(__name__)

_strong_llm = ChatGoogleGenerativeAI(model=settings.strong_model, temperature=0.0, google_api_key=settings.google_api_key)

_parser = PydanticOutputParser(pydantic_object=DecomposedRule)

_DECOMPOSE_PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     "You are a compliance engineering expert. Decompose the given regulatory rule text "
     "into machine-checkable violation conditions. Each condition must specify: "
     "data_category, trigger, check_type (metadata|sql|regex|llm_fallback), "
     "severity, and a remediation_template. "
     "For sql check_type conditions, include a sql_check_template using {{table}} and {{column}} placeholders "
     "that queries information_schema.COLUMNS. "
     "Be precise — these conditions will be matched programmatically at scan time.\n\n"
     "{format_instructions}"),
    ("human",
     "Rule ID: {rule_id}\nSource: {source_doc}\nArticle: {article_ref}\n\nRULE TEXT:\n{rule_text}"),
])

def _make_rule_id(source_doc: str, article_ref: str, span_text: str) -> str:
    prefix = Path(source_doc).stem.upper().replace(" ", "-")
    raw = f"{source_doc}::{article_ref}::{span_text.strip().lower()}"
    hash_suffix = hashlib.md5(raw.encode()).hexdigest()[:8]
    return f"{prefix}-{article_ref.replace(' ', '')}-{hash_suffix}"


def _decompose_with_retry(span: dict, max_retries: int = 3) -> DecomposedRule | None:
    """
    Structured output with validation-feedback retry.
    On parse failure, feeds the error back to the LLM as context
    (not a blind retry) — the model self-corrects.
    """
    structured_model = _strong_llm.with_structured_output(DecomposedRule, include_raw=True)
    rule_id = _make_rule_id(
        span["source_doc"],
        span.get("article_ref", "X"),
        span["span_text"],
    )

    last_error = None
    for attempt in range(max_retries):
        try:
            messages = _DECOMPOSE_PROMPT.format_messages(
                rule_id=rule_id,
                source_doc=span["source_doc"],
                article_ref=span.get("article_ref", ""),
                rule_text=span["span_text"],
                format_instructions=_parser.get_format_instructions(),
            )
            if last_error and attempt > 0:
                messages.append(
                    HumanMessage(content=f"Your previous output failed validation: {last_error}. Please fix and return valid JSON.")
                )
            result = structured_model.invoke(messages)
            if result["parsed"]:
                return result["parsed"]
            last_error = str(result.get("parsing_error", "Unknown parse error"))
            logger.warning("Decomposition attempt %d failed: %s", attempt + 1, last_error)
        except Exception as e:
            last_error = str(e)
            logger.warning("Decomposition attempt %d exception: %s", attempt + 1, e)

    logger.error("Decomposition failed after %d attempts for span: %s", max_retries, span.get("article_ref"))
    return None


@tool
def decompose_rule_span(span: dict) -> dict | None:
    """
    Decompose a single rule span (from Pass-2 extraction) into a DecomposedRule
    with machine-checkable ViolationConditions.
    This is the LLM cost boundary — called once per rule, never at scan time.
    Returns serialised DecomposedRule dict or None on failure.
    """
    result = _decompose_with_retry(span)
    if result is None:
        return None
    return result.model_dump()
