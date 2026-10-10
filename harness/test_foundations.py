"""Phase 1 self-check: python harness/test_foundations.py   (no framework; asserts only)."""
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "grader"))
import docreader  # noqa: E402
import extract as G  # noqa: E402  (the grader's reader: the ground truth for where a quote is found)
import facts  # noqa: E402
import llm  # noqa: E402


def test_normalizers():
    assert facts.invoice_id("TX-US 2035-0210") == "TX-US2035-0210"
    assert facts.invoice_id("#txi/35-36/0030:") == "TXI/35-36/0030"
    assert facts.money("INR45,219.11") == Decimal("45219.11") and facts.money("USD 12.65") == Decimal("12.65")
    assert facts.usd_str(Decimal("2.675")) == "2.68" and facts.usd_str(Decimal("0.005")) == "0.01"   # half up
    assert facts.received_by("2035-05-31T23:59:59Z", "2035-05-31")
    assert not facts.received_by("2035-06-01T00:00:00Z", "2035-05-31")


def test_reader_matches_grader_everywhere():
    n = 0
    for mb in ("data/scenarios", "data/exam", "data/examples"):
        for batch in sorted((ROOT / mb).rglob("batch_*")):
            if not batch.is_dir():
                continue
            for d in docreader.load_batch(batch):
                want = G.pages_of(d["bytes"], d["kind"]) if d["kind"] != "email" else None
                if want is not None:
                    assert d["pages"] == want, d["doc_id"]
                n += 1
    assert n > 200, n


def test_example_quotes_are_found():
    docs = {d["doc_id"]: d for d in docreader.load_batch(ROOT / "data/examples/overcharge/docs/batch_01")}
    inv = docs["batch_01/0003.eml#TX-US-2032-5394.pdf"]["pages"][0]
    assert "1 TE56915687 2031-12-25 Air freight USE2-INE2 241 kg 6.69 1612.29" in inv
    trk = docs["batch_01/0005.eml#tracking-2031-07-01.xlsx"]["pages"][0]
    assert "TE56915687 | PICKED_UP | 2031-12-25 | USE2 | -" in trk
    assert docs["batch_01/0003.eml"]["received_at"] == "2032-01-03T" + docs["batch_01/0003.eml"]["received_at"][11:]
    ln = [i for i, t in enumerate(inv.split("\n"), 1) if "TE56915687 2031-12-25 Air freight" in t][0]
    assert docreader.pick_lines(inv, [ln]) == "1 TE56915687 2031-12-25 Air freight USE2-INE2 241 kg 6.69 1612.29"


def test_one_scan_per_mailbox():
    for mb in ("data/scenarios", "data/exam"):
        scans = [d["doc_id"] for b in sorted((ROOT / mb).glob("batch_*")) for d in docreader.load_batch(b)
                 if d["is_scan"]]
        assert len(scans) == 1, (mb, scans)
    d = next(d for b in sorted((ROOT / "data/exam").glob("batch_*")) for d in docreader.load_batch(b) if d["is_scan"])
    assert docreader.render_pdf_page_png(d["bytes"], 1)[:4] == b"\x89PNG"


def test_llm_cache():
    class Fake:
        n = 0

        class messages:
            @staticmethod
            def create(**kw):
                Fake.n += 1
                blk = type("B", (), {"type": "tool_use", "input": {"x": 1}})()
                return type("R", (), {"content": [blk], "usage": None})()
    with tempfile.TemporaryDirectory() as t:
        m = llm.LLM(t, client=Fake())
        a = m.structured(model="m", system="s", content="c", schema={"type": "object"})
        b = m.structured(model="m", system="s", content="c", schema={"type": "object"})
        assert a == b == {"x": 1} and Fake.n == 1 and m.calls == 1 and m.hits == 1
        assert llm.LLM(t, client=Fake()).structured(model="m", system="s", content="c",
                                                    schema={"type": "object"}) == {"x": 1} and Fake.n == 1


def test_ingest_idempotent():
    import os
    env = dict(os.environ, HARNESS_CACHE_DIR=str(ROOT / ".cache" / "llm"))      # shared dev cache: no repeat model calls
    with tempfile.TemporaryDirectory() as t:
        cmd = [sys.executable, str(ROOT / "harness/harness.py"), "ingest",
               str(ROOT / "data/examples/overcharge/docs/batch_01"), "--state-dir", t]
        first = subprocess.run(cmd, capture_output=True, text=True, env=env)
        second = subprocess.run(cmd, capture_output=True, text=True, env=env)
        assert first.returncode == 0 and "8 new document(s), 0 failed" in first.stderr, first.stderr
        assert second.returncode == 0 and "0 new document(s)" in second.stderr, second.stderr
        assert len(facts.State(t).all("docs")) == 8 and len(facts.State(t).all("facts")) == 8


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
