"""
tests.test_rag — local document store: chunking + TF-IDF retrieval.
"""

from backend.rag.store import DocumentStore


def test_add_and_retrieve(tmp_path):
    store = DocumentStore(document_dir=tmp_path)
    store.add_document(
        "space.txt",
        "The James Webb telescope observes infrared light from distant galaxies. "
        "It orbits the Sun near the second Lagrange point.",
    )
    store.add_document(
        "cooking.txt",
        "To bake bread you need flour, water, yeast and salt. Knead the dough "
        "for ten minutes and let it rise for one hour.",
    )

    results = store.retrieve("telescope infrared galaxies")
    assert results, "expected retrieval hits"
    assert results[0].chunk.doc_name == "space.txt"
    assert results[0].score > 0

    results = store.retrieve("bread dough yeast")
    assert results[0].chunk.doc_name == "cooking.txt"


def test_empty_store_retrieval(tmp_path):
    store = DocumentStore(document_dir=tmp_path)
    assert store.retrieve("anything") == []
    assert store.document_count == 0


def test_long_text_is_chunked(tmp_path):
    store = DocumentStore(document_dir=tmp_path)
    long_text = "\n\n".join(f"Paragraph {i} about quantum computing and qubits." for i in range(30))
    meta = store.add_document("quantum.txt", long_text)
    assert meta["chunks"] > 1
    assert store.chunk_count == meta["chunks"]


def test_persistence_across_instances(tmp_path):
    store = DocumentStore(document_dir=tmp_path)
    store.add_document("kb.txt", "The office wifi password is hunter2 according to the IT handbook.")
    # A brand-new store instance reloads from disk.
    store2 = DocumentStore(document_dir=tmp_path)
    assert store2.document_count == 1
    hits = store2.retrieve("wifi password handbook")
    assert hits and "hunter2" in hits[0].chunk.text
