import pytest

from app.clipper import ChunkIndex, build_filtergraph

pytestmark = pytest.mark.unit


def make_index():
    idx = ChunkIndex()
    for name in ("c0", "c1", "c2"):
        idx.add(name, 30)
    return idx


def test_pieces_span_chunks_exactly():
    pieces = make_index().pieces(20, 70)
    assert [(p, round(s, 3), round(d, 3)) for p, s, d in pieces] == [("c0", 20, 10), ("c1", 0, 30), ("c2", 0, 10)]
    assert sum(d for _, _, d in pieces) == pytest.approx(50)


def test_pieces_inside_one_chunk_and_out_of_range():
    idx = make_index()
    assert idx.pieces(35, 50) == [("c1", 5, 15)]
    assert idx.pieces(200, 220) == []
    assert idx.pieces(80, 120) == [("c2", 20, 10)]  # clamps to what exists
    assert idx.total == 90


def test_filtergraph_shapes():
    g = build_filtergraph(3, "blur")
    assert "concat=n=3:v=1:a=1" in g and "boxblur" in g and "pad=" not in g  # same-size chunks: no re-scaling
    assert "[vout]" in g and "[aout]" in g
    mixed = build_filtergraph(2, "blur", norm=(1920, 1080))
    assert mixed.count("scale=1920:1080") == 2 and mixed.count("pad=1920:1080") == 2
    c = build_filtergraph(1, "crop")
    assert "crop=1080:1920" in c and "boxblur" not in c
