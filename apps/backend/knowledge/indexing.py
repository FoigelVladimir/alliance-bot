import math

from jobs.services import LeaseLost, enqueue

from .embeddings import DIMENSIONS, MODEL_REVISION, get_embedder, split_text
from .models import DocumentFragment, FragmentGeneration, MaterialVersion
from .services import locked_material

TASK_TYPE = "knowledge.index_snapshot"
BATCH_SIZE = 8


def enqueue_index(snapshot):
    task = enqueue(TASK_TYPE, snapshot.preparation_id, {"snapshot_id": snapshot.pk}, priority=-10)
    return FragmentGeneration.objects.create(snapshot=snapshot, task=task, model_revision=MODEL_REVISION)


def index_snapshot_task(task):
    generation = FragmentGeneration.objects.select_related("snapshot__preparation__version").get(
        task=task, snapshot_id=task.payload["snapshot_id"],
    )
    snapshot = generation.snapshot
    preparation = snapshot.preparation
    version = preparation.version
    if (version.current_preparation_id != preparation.pk or version.draft_revision != preparation.draft_revision
            or version.sealed_at is not None or generation.model_revision != MODEL_REVISION):
        raise LeaseLost(task.pk)

    embedder = get_embedder()
    fragments = split_text(snapshot.prepared_text, embedder.token_count)
    if not fragments or "".join(fragments) != snapshot.prepared_text:
        raise ValueError("Incomplete fragment generation")
    vectors = []
    for offset in range(0, len(fragments), BATCH_SIZE):
        batch = fragments[offset:offset + BATCH_SIZE]
        output = embedder.passages(batch)
        if len(output) != len(batch) or any(
            len(vector) != DIMENSIONS or not math.isclose(math.hypot(*vector), 1.0, rel_tol=1e-5)
            for vector in output
        ):
            raise ValueError("Incomplete or invalid embeddings")
        vectors.extend(output)

    def save_generation():
        locked_material(version.material_id)
        current = MaterialVersion.objects.select_for_update().get(pk=version.pk)
        if (current.current_preparation_id != preparation.pk or current.draft_revision != preparation.draft_revision
                or current.sealed_at is not None):
            raise LeaseLost(task.pk)
        DocumentFragment.objects.bulk_create([
            DocumentFragment(
                document_id=current.material_id, version=current, prepared_snapshot=snapshot,
                position=position, heading=current.title, content=content, embedding=vector,
            )
            for position, (content, vector) in enumerate(zip(fragments, vectors, strict=True))
        ])
        generation.fragment_count = len(fragments)
        generation.save(update_fields=["fragment_count"])
        return {"fragment_count": generation.fragment_count}

    return save_generation
