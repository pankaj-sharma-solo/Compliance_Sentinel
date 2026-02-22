from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct,
    Filter, FieldCondition, MatchValue, MatchAny,
    UpdateStatus
)
from fastembed import TextEmbedding
from sentinel.config import settings
import uuid
import logging

logger = logging.getLogger(__name__)

_client: QdrantClient | None = None
_embedder: TextEmbedding | None = None


def get_client() -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(url=settings.qdrant_url)
    return _client


def get_embedder() -> TextEmbedding:
    global _embedder
    if _embedder is None:
        _embedder = TextEmbedding()
    return _embedder


def ensure_collection():
    """Create collection if it doesn't exist — idempotent."""
    client = get_client()
    existing = [c.name for c in client.get_collections().collections]
    if settings.qdrant_collection not in existing:
        client.create_collection(
            collection_name=settings.qdrant_collection,
            vectors_config=VectorParams(size=1536, distance=Distance.COSINE),
        )
        logger.info("Created Qdrant collection: %s", settings.qdrant_collection)


# ── Role 1: Upsert a rule after ingestion ────────────────────────────────────

def upsert_rule(rule_id: str, rule_text: str, metadata: dict):
    """Embed rule text and upsert to Qdrant. metadata must contain rule_id, status, etc."""
    ensure_collection()
    vector = list(get_embedder().embed(rule_text))
    point = PointStruct(
        id=str(uuid.uuid5(uuid.NAMESPACE_DNS, rule_id)),
        vector=vector[0].tolist(),
        payload={"rule_id": rule_id, **metadata},
    )
    result = get_client().upsert(collection_name=settings.qdrant_collection, points=[point])
    return result.status == UpdateStatus.COMPLETED


# ── Role 1: Retrieve top-k relevant rules for a table schema context ─────────

def retrieve_relevant_rules(schema_context: str, top_k: int = None, status_filter: str = "ACTIVE") -> list[dict]:
    """
    Embed schema context (table name + column names + categories) and
    return top-k matching active rules. Reduces O(n*m) to O(k*m) at scan time.
    """
    k = top_k or settings.max_relevant_rules_per_table
    ensure_collection()
    vector = list(get_embedder().embed(schema_context))[0].tolist()
    results = get_client().query_points(
        collection_name=settings.qdrant_collection,
        query=vector,
        limit=k,
        query_filter=Filter(
            must=[FieldCondition(key="status", match=MatchValue(value=status_filter))]
        ),
        with_payload=True,
    )
    return [{"rule_id": r.payload["rule_id"], "score": r.score, **r.payload} for r in results.points]


# ── Role 2: Version reconciliation — find nearest existing rule ───────────────
def find_nearest_rule(rule_text: str) -> dict | None:
    """
    On new PDF upload, embed the new rule and find the most similar
    existing rule. Returns {rule_id, score} or None if collection empty.
    """
    ensure_collection()

    # embed() returns a generator — consume it with next()
    vector = list(get_embedder().embed(rule_text))[0].tolist()

    results = get_client().query_points(
        collection_name=settings.qdrant_collection,
        query=vector,
        limit=1,
        with_payload=True,
    )

    points = results.points   # query_points returns a QueryResponse, not a list
    if not points:
        return None
    return {"rule_id": points[0].payload["rule_id"], "score": points[0].score}



def deprecate_rule_in_vector_store(rule_id: str):
    """
    Update metadata status to 'DEPRECATED'. Never delete —
    historical audit queries must still surface deprecated rules.
    """
    point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, rule_id))
    get_client().set_payload(
        collection_name=settings.qdrant_collection,
        payload={"status": "DEPRECATED"},
        points=[point_id],
    )


# ── Role 3: User semantic search (Policy Library UI) ─────────────────────────
def semantic_search(query: str, top_k: int = 10, filters: dict | None = None) -> list[dict]:
    """
    Natural language search against rule library.
    filters: optional {status, regulation_type, severity}
    """
    ensure_collection()

    # Fix 1: FastEmbed has no embed_query() — use embed() directly
    vector = list(get_embedder().embed(query))[0].tolist()

    must_conditions = []
    if filters:
        for key, value in filters.items():
            if isinstance(value, list):
                must_conditions.append(FieldCondition(key=key, match=MatchAny(any=value)))
            else:
                must_conditions.append(FieldCondition(key=key, match=MatchValue(value=value)))

    # Fix 2: .search() deprecated — use .query_points()
    results = get_client().query_points(
        collection_name=settings.qdrant_collection,
        query=vector,
        limit=top_k,
        query_filter=Filter(must=must_conditions) if must_conditions else None,
        with_payload=True,
    )

    return [{"rule_id": r.payload["rule_id"], "score": r.score, **r.payload} for r in results.points]

