import math
from dataclasses import dataclass

from django.db import connection, transaction

from accounts.policies import quarantine_incident

from .embeddings import DIMENSIONS, get_embedder
from .models import DocumentFragment, Material

CANDIDATE_LIMIT = 30
RRF_CONSTANT = 60


@dataclass(frozen=True)
class SearchHit:
    fragment_id: int
    material_id: int
    version_id: int
    snapshot_id: int
    heading: str
    content: str
    score: float


def hybrid_search(question, *, access_level=0, limit=6):
    if not question.strip() or quarantine_incident():
        return []
    vector, = get_embedder().encode(["query: " + question])
    return rank_fragments(question, vector, access_level=access_level, limit=limit)


def rank_fragments(question, vector, *, access_level=0, limit=6):
    if len(vector) != DIMENSIONS or not math.isfinite(math.hypot(*vector)) or not math.hypot(*vector):
        raise ValueError("Invalid search embedding")
    if not 1 <= limit <= CANDIDATE_LIMIT or not 0 <= access_level <= 32767:
        raise ValueError("Invalid search bounds")
    if not question.strip() or quarantine_incident():
        return []
    vector_literal = "[" + ",".join(str(float(component)) for component in vector) + "]"
    # Both ranking branches read the same PostgreSQL statement snapshot.
    with connection.cursor() as cursor:
        cursor.execute("""
            WITH eligible AS NOT MATERIALIZED (
                SELECT f.*
                FROM kb_document_fragment f
                JOIN kb_document_version v ON v.id = f.version_id AND v.material_id = f.document_id
                WHERE f.is_active AND v.is_published AND NOT v.publication_verification_required
                  AND v.published_prepared_snapshot_id = f.prepared_snapshot_id AND f.access_level <= %(access)s
            ), query AS (
                SELECT websearch_to_tsquery('russian', %(question)s) AS terms, %(embedding)s::vector AS embedding
            ), lexical AS (
                SELECT id, row_number() OVER (ORDER BY ts_rank_cd(search_vector, terms) DESC, id) AS rank
                FROM eligible CROSS JOIN query WHERE search_vector @@ terms
                ORDER BY rank LIMIT %(candidates)s
            ), semantic AS (
                SELECT id, row_number() OVER (ORDER BY eligible.embedding <=> query.embedding, id) AS rank
                FROM eligible CROSS JOIN query
                ORDER BY rank LIMIT %(candidates)s
            ), scores AS (
                SELECT id, sum(weight / (%(rrf)s + rank)) AS score
                FROM (
                    SELECT id, rank, 0.4 AS weight FROM lexical
                    UNION ALL
                    SELECT id, rank, 0.6 AS weight FROM semantic
                ) AS ranks GROUP BY id
            )
            SELECT f.id, f.document_id, f.version_id, f.prepared_snapshot_id, f.heading, f.content, s.score
            FROM scores s JOIN eligible f ON f.id = s.id
            ORDER BY s.score DESC, f.id LIMIT %(limit)s
        """, {
            "question": question, "embedding": vector_literal, "access": access_level,
            "candidates": CANDIDATE_LIMIT, "rrf": RRF_CONSTANT, "limit": limit,
        })
        return [SearchHit(*row[:-1], float(row[-1])) for row in cursor.fetchall()]


def sources_are_current(hits, *, access_level=0):
    """Hold material locks until the caller commits its admission decision."""
    if not connection.in_atomic_block:
        raise RuntimeError("Source verification requires the caller's transaction")
    material_ids = {hit.material_id for hit in hits}
    list(Material.objects.select_for_update().filter(pk__in=material_ids).order_by("pk"))
    if quarantine_incident():
        return False
    current = set(DocumentFragment.objects.filter(
        pk__in={hit.fragment_id for hit in hits}, document_id__in=material_ids, is_active=True,
        version__is_published=True, version__publication_verification_required=False, access_level__lte=access_level,
    ).values_list("pk", "document_id", "version_id", "prepared_snapshot_id", "version__published_prepared_snapshot_id",
                  "version__material_id"))
    return all((hit.fragment_id, hit.material_id, hit.version_id, hit.snapshot_id, hit.snapshot_id, hit.material_id)
               in current for hit in hits)
