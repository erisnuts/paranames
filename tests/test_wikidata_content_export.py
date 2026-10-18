import bz2
import csv
import gzip
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

import orjson
import pytest
from click.testing import CliRunner

import paranames.io.wikidata_content_export as ce

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------


def claim(value_id=None, rank="normal", prop="P31", snaktype="value"):
    mainsnak = {"snaktype": snaktype, "property": prop}
    if value_id is not None and snaktype == "value":
        mainsnak["datavalue"] = {
            "value": {
                "entity-type": "item",
                "numeric-id": int(value_id[1:]),
                "id": value_id,
            },
            "type": "wikibase-entityid",
        }
    return {"mainsnak": mainsnak, "type": "statement", "rank": rank}


def item(qid, labels=None, p31=(), p279=(), sitelinks=None, extra_claims=None):
    claims = {}
    if p31:
        claims["P31"] = [c if isinstance(c, dict) else claim(c) for c in p31]
    if p279:
        claims["P279"] = [
            c if isinstance(c, dict) else claim(c, prop="P279") for c in p279
        ]
    if extra_claims:
        claims.update(extra_claims)
    return {
        "type": "item",
        "id": qid,
        "labels": {l: {"language": l, "value": v} for l, v in (labels or {}).items()}
        or [],
        "descriptions": [],
        "aliases": [],
        "claims": claims or [],
        "sitelinks": {
            s: {"site": s, "title": t, "badges": []}
            for s, t in (sitelinks or {}).items()
        }
        or [],
    }


def page(title, ns, model, text, page_id=1):
    return (
        f"<page><title>{escape(title)}</title><ns>{ns}</ns><id>{page_id}</id>"
        f"<revision><id>{page_id}0</id><model>{model}</model>"
        f"<format>application/json</format>"
        f'<text bytes="{len(text)}" xml:space="preserve">{escape(text)}</text>'
        f"</revision></page>"
    )


def write_part(path, pages, ns_version="0.11"):
    xml = (
        f'<mediawiki xmlns="http://www.mediawiki.org/xml/export-{ns_version}/" '
        f'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" version="{ns_version}" xml:lang="en">'
        "<siteinfo><sitename>Wikidata</sitename></siteinfo>"
        + "".join(pages)
        + "</mediawiki>"
    )
    with bz2.open(path, "wt", encoding="utf-8") as f:
        f.write(xml)


def item_page(it, i):
    return page(it["id"], 0, "wikibase-item", json.dumps(it), page_id=i)


# Classes: Q82794 (LOC root) <- Q1001 <- Q1002 <- Q1003 (depth 3)
#          Q43229 (ORG root) <- Q2001
#          Q3001 -> Q82794 only through a deprecated edge (not LOC)
#          Q3002: preferred P279 -> Q4000 (unrelated), normal -> Q82794 (not LOC)
#          Q3003: preferred P279 -> Q43229 (ORG), normal -> Q4000
#          cycle: Q5001 <-> Q5002, Q5001 -> Q43229
PART1 = [
    item("Q1001", p279=["Q82794"]),
    item("Q1002", p279=["Q1001"]),
    item("Q1003", p279=["Q1002"]),
    item("Q2001", p279=["Q43229"]),
    item("Q3001", p279=[claim("Q82794", rank="deprecated", prop="P279")]),
    item(
        "Q3002",
        p279=[
            claim("Q4000", rank="preferred", prop="P279"),
            claim("Q82794", rank="normal", prop="P279"),
        ],
    ),
    item(
        "Q3003",
        p279=[
            claim("Q43229", rank="preferred", prop="P279"),
            claim("Q4000", rank="normal", prop="P279"),
        ],
    ),
    item("Q5001", p279=["Q5002", "Q43229"]),
    item("Q5002", p279=["Q5001"]),
]

PART2 = [
    # PER with en; one excluded language (de-formal) and a de label
    item(
        "Q10",
        labels={
            "en": "Ada Lovelace",
            "de": "Ada Lovelace (DE)",
            "de-formal": "X",
            "fr": "Ada",
        },
        p31=["Q5"],
        sitelinks={"enwiki": "Ada Lovelace", "dewiki": "Ada Lovelace"},
    ),
    # PER with only mul (and fr): gets name = mul, an en row, no mul row
    item("Q11", labels={"mul": "Douglas Adams", "fr": "Douglas Adams FR"}, p31=["Q5"]),
    # P31 novalue claim next to Q5: whole P31 set empty -> dropped
    item(
        "Q12",
        labels={"en": "No Value"},
        p31=["Q5", claim(snaktype="novalue")],
    ),
    # LOC through depth-3 chain
    item("Q13", labels={"en": "Deep Place", "de": "Tiefer Ort"}, p31=["Q1003"]),
    # deprecated edge only -> not LOC
    item("Q14", labels={"en": "Deprecated Place"}, p31=["Q3001"]),
    # preferred beats normal -> not LOC
    item("Q15", labels={"en": "Preferred Other"}, p31=["Q3002"]),
    # preferred -> ORG
    item("Q16", labels={"en": "Preferred Org"}, p31=["Q3003"]),
    # cycle under ORG
    item("Q17", labels={"en": "Cycle Org"}, p31=["Q5002"]),
    # multi-type: PER and LOC
    item("Q18", labels={"en": "Multi", "de": "Multi DE"}, p31=["Q5", "Q1001"]),
    # no en, no mul -> dropped
    item("Q19", labels={"fr": "Seulement"}, p31=["Q5"]),
    # direct ORG root, no de label
    item("Q20", labels={"en": "Root Org"}, p31=["Q43229"]),
]

OTHER_PAGES = [
    page(
        "Property:P31",
        120,
        "wikibase-property",
        json.dumps({"type": "property", "id": "P31"}),
        900,
    ),
    page("Wikidata:Main Page", 4, "wikitext", "hello", 901),
    page("Q999", 0, "wikitext", "not an item", 902),
]


@pytest.fixture
def parts_dir(tmp_path):
    d = tmp_path / "parts"
    d.mkdir()
    write_part(
        d / "part-1.xml.bz2", [item_page(it, i) for i, it in enumerate(PART1, 1)]
    )
    write_part(
        d / "part-2.xml.bz2",
        [item_page(it, i) for i, it in enumerate(PART2, 100)] + OTHER_PAGES,
        ns_version="0.10",
    )
    return d


def run_pipeline(tmp_path, parts_dir, languages="en", mul_fallback="en"):
    jsonl = tmp_path / f"jsonl-{languages}"
    jsonl.mkdir()
    all_stats = {}
    for part in sorted(parts_dir.glob("*.xml.bz2")):
        out = jsonl / (part.name.replace(".xml.bz2", ".jsonl.gz"))
        all_stats[part.name] = ce.extract_part(str(part), str(out), languages=languages)
    closures = tmp_path / f"closures-{languages}"
    ce.build_closures(str(jsonl), str(closures))
    out_dir = tmp_path / f"out-{languages}"
    dump_stats = ce.dump_tsv(
        str(jsonl),
        str(closures),
        str(out_dir),
        languages=languages,
        mul_fallback=mul_fallback,
    )
    return jsonl, closures, out_dir, all_stats, dump_stats


def read_tsv(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def rows_by_id(rows):
    out = {}
    for r in rows:
        out.setdefault(r["wikidata_id"], []).append(r)
    return out


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_p31_novalue_empties_set():
    claims = {"P31": [claim("Q5"), claim(snaktype="novalue")]}
    assert ce.parse_p31(claims) == []


def test_p31_reads_all_ranks():
    claims = {"P31": [claim("Q5", rank="deprecated"), claim("Q6", rank="preferred")]}
    assert ce.parse_p31(claims) == ["Q5", "Q6"]


def test_p31_numeric_id_only():
    c = claim("Q5")
    del c["mainsnak"]["datavalue"]["value"]["id"]
    assert ce.parse_p31({"P31": [c]}) == ["Q5"]


def test_p279_truthy():
    claims = {
        "P279": [
            claim("Q1", rank="preferred", prop="P279"),
            claim("Q2", rank="normal", prop="P279"),
            claim("Q3", rank="deprecated", prop="P279"),
        ]
    }
    assert ce.parse_p279_truthy(claims) == ["Q1"]
    claims["P279"] = claims["P279"][1:]
    assert ce.parse_p279_truthy(claims) == ["Q2"]
    claims["P279"] = claims["P279"][1:]
    assert ce.parse_p279_truthy(claims) == []


def test_closure_handles_cycle():
    children = {"R": {"A"}, "A": {"B"}, "B": {"A", "C"}}
    assert ce.descendant_closure(children, "R") == {"R", "A", "B", "C"}


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------


def test_extract_skips_and_counts(tmp_path, parts_dir):
    out = tmp_path / "p2.jsonl.gz"
    stats = ce.extract_part(str(parts_dir / "part-2.xml.bz2"), str(out))
    assert stats["pages_seen"] == len(PART2) + 3
    assert stats["skipped_namespace"] == 2
    assert stats["skipped_model"] == 1
    assert stats["item_pages"] == len(PART2)
    assert stats["items_written"] == len(PART2)
    with gzip.open(out, "rb") as f:
        records = [orjson.loads(l) for l in f]
    ids = [r["id"] for r in records]
    assert "P31" not in ids and "Q999" not in ids
    q10 = next(r for r in records if r["id"] == "Q10")
    # English-only extract keeps en and mul only
    assert set(q10["labels"]) == {"en"}
    assert q10["sitelinks"] == {"enwiki": "Ada Lovelace", "dewiki": "Ada Lovelace"}
    q11 = next(r for r in records if r["id"] == "Q11")
    assert set(q11["labels"]) == {"mul"}
    q12 = next(r for r in records if r["id"] == "Q12")
    assert q12["p31"] == [] and q12["labels"] == {}


def test_extract_all_languages_keeps_everything(tmp_path, parts_dir):
    out = tmp_path / "p2.jsonl.gz"
    ce.extract_part(str(parts_dir / "part-2.xml.bz2"), str(out), languages="all")
    with gzip.open(out, "rb") as f:
        q10 = next(r for r in map(orjson.loads, f) if r["id"] == "Q10")
    assert set(q10["labels"]) == {"en", "de", "de-formal", "fr"}


def test_sha256_ok_and_mismatch(tmp_path, parts_dir):
    part = parts_dir / "part-1.xml.bz2"
    digest = hashlib.sha256(part.read_bytes()).hexdigest()
    good = tmp_path / "SHA256SUMS"
    good.write_text(f"{digest}  part-1.xml.bz2\n")
    stats = ce.extract_part(str(part), str(tmp_path / "ok.jsonl.gz"), str(good))
    assert stats["sha256_verified"] == 1

    bad = tmp_path / "BAD_SHA256SUMS"
    bad.write_text(f"{'0' * 64}  part-1.xml.bz2\n")
    out = tmp_path / "bad.jsonl.gz"
    with pytest.raises(ce.ChecksumError):
        ce.extract_part(str(part), str(out), str(bad))
    assert not out.exists() and not Path(str(out) + ".tmp").exists()

    result = CliRunner().invoke(
        ce.cli,
        ["extract", "-i", str(part), "-o", str(out), "--sha256sums", str(bad)],
    )
    assert result.exit_code != 0
    assert "SHA-256 mismatch" in result.output


# ---------------------------------------------------------------------------
# types + dump
# ---------------------------------------------------------------------------


def test_closures(tmp_path, parts_dir):
    _, closures, _, _, _ = run_pipeline(tmp_path, parts_dir)
    c = ce.read_closures(str(closures))
    assert c["LOC"] == {"Q82794", "Q1001", "Q1002", "Q1003"}
    assert c["ORG"] == {"Q43229", "Q2001", "Q3003", "Q5001", "Q5002"}


def test_english_only_run(tmp_path, parts_dir):
    _, _, out, _, stats = run_pipeline(tmp_path, parts_dir, languages="en")
    rows = read_tsv(out / "combined.tsv")
    assert {r["language"] for r in rows} == {"en"}
    by_id = rows_by_id(rows)

    # typed entities only
    assert set(by_id) == {"Q10", "Q11", "Q13", "Q16", "Q17", "Q18", "Q20"}

    # mul fallback: name and en row from mul, no mul row
    assert by_id["Q11"] == [
        {
            "wikidata_id": "Q11",
            "name": "Douglas Adams",
            "label": "Douglas Adams",
            "language": "en",
            "type": "PER",
        }
    ]
    assert stats["mul_as_english"] == 1

    assert [r["type"] for r in by_id["Q13"]] == ["LOC"]
    assert [r["type"] for r in by_id["Q16"]] == ["ORG"]
    assert [r["type"] for r in by_id["Q17"]] == ["ORG"]
    assert sorted(r["type"] for r in by_id["Q18"]) == ["LOC", "PER"]
    for dropped in ("Q12", "Q14", "Q15", "Q19"):
        assert dropped not in by_id


def test_mul_fallback_can_be_disabled(tmp_path, parts_dir):
    _, _, out, _, _ = run_pipeline(tmp_path, parts_dir, languages="en", mul_fallback="")
    by_id = rows_by_id(read_tsv(out / "combined.tsv"))
    assert "Q11" not in by_id


def test_two_language_run(tmp_path, parts_dir):
    _, _, out, _, _ = run_pipeline(tmp_path, parts_dir, languages="en,de")
    rows = read_tsv(out / "combined.tsv")
    assert {r["language"] for r in rows} == {"en", "de"}
    by_id = rows_by_id(rows)
    # item with a de label
    assert {(r["language"], r["label"]) for r in by_id["Q13"]} == {
        ("en", "Deep Place"),
        ("de", "Tiefer Ort"),
    }
    # item without a de label: en row only; mul fallback does not apply to de
    assert {r["language"] for r in by_id["Q20"]} == {"en"}
    assert {r["language"] for r in by_id["Q11"]} == {"en"}
    # excluded language never appears even if requested via all
    assert "de-formal" not in {r["language"] for r in rows}


def test_all_languages_run(tmp_path, parts_dir):
    _, _, out, _, _ = run_pipeline(tmp_path, parts_dir, languages="all")
    rows = read_tsv(out / "combined.tsv")
    langs = {r["language"] for r in rows}
    assert "de-formal" not in langs  # data/exclude_these_languages.txt
    assert "mul" not in langs
    assert {"en", "de", "fr"} <= langs
    by_id = rows_by_id(rows)
    assert {(r["language"], r["label"]) for r in by_id["Q11"]} == {
        ("en", "Douglas Adams"),
        ("fr", "Douglas Adams FR"),
    }
    # multi-type entity: two full row sets
    q18 = by_id["Q18"]
    assert len(q18) == 4
    assert {(r["type"], r["language"]) for r in q18} == {
        ("PER", "en"),
        ("PER", "de"),
        ("LOC", "en"),
        ("LOC", "de"),
    }


def test_tsv_format_matches_upstream(tmp_path, parts_dir):
    _, _, out, _, _ = run_pipeline(tmp_path, parts_dir, languages="all")
    # per-type files: csv.DictWriter excel dialect (CRLF), header first
    per = (out / "PER.tsv").read_bytes()
    assert per.startswith(b"wikidata_id\tname\tlabel\tlanguage\ttype\r\n")
    # combined: one header, LOC then ORG then PER (glob order), LF
    combined = (out / "combined.tsv").read_text(encoding="utf-8")
    lines = combined.split("\n")
    assert lines[0] == "wikidata_id\tname\tlabel\tlanguage\ttype"
    assert "\r" not in combined
    assert combined.count("wikidata_id\tname") == 1
    types_in_order = [l.split("\t")[4] for l in lines[1:] if l]
    assert types_in_order == sorted(types_in_order, key=["LOC", "ORG", "PER"].index)

    sitelinks = read_tsv(out / "sitelinks.tsv")
    assert {"wikidata_id": "Q10", "site": "dewiki"} in sitelinks
    assert all(r["wikidata_id"] in {"Q10"} for r in sitelinks)


def test_postprocess_accepts_output(tmp_path, parts_dir):
    _, _, out, _, _ = run_pipeline(tmp_path, parts_dir, languages="all")
    post = tmp_path / "combined_postprocessed.tsv"
    # same arguments as recipes/dump.sh:139-141 (no disambiguation, no collapse)
    result = subprocess.run(
        [
            sys.executable,
            "paranames/io/postprocess.py",
            "-i",
            str(out / "combined.tsv"),
            "-o",
            str(post),
            "-f",
            "tsv",
            "-m",
            "1",
            "--should-remove-parentheses",
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    rows = read_tsv(post)
    assert list(rows[0].keys()) == ["wikidata_id", "eng", "label", "language", "type"]
    # parentheses removed from label
    q10_de = [r for r in rows if r["wikidata_id"] == "Q10" and r["language"] == "de"]
    assert q10_de and q10_de[0]["label"] == "Ada Lovelace"


def test_cli_end_to_end(tmp_path, parts_dir):
    runner = CliRunner()
    jsonl = tmp_path / "j"
    jsonl.mkdir()
    for part in parts_dir.glob("*.xml.bz2"):
        r = runner.invoke(
            ce.cli,
            ["extract", "-i", str(part), "-o", str(jsonl / (part.stem + ".jsonl.gz"))],
        )
        assert r.exit_code == 0, r.output
    r = runner.invoke(ce.cli, ["types", "-i", str(jsonl), "-o", str(tmp_path / "c")])
    assert r.exit_code == 0, r.output
    r = runner.invoke(
        ce.cli,
        [
            "dump",
            "-i",
            str(jsonl),
            "-c",
            str(tmp_path / "c"),
            "-o",
            str(tmp_path / "o"),
        ],
    )
    assert r.exit_code == 0, r.output
    assert (tmp_path / "o" / "combined.tsv").exists()


# ---------------------------------------------------------------------------
# recipes/paranames_content_export.sh
# ---------------------------------------------------------------------------


def _icu_available():
    try:
        import icu  # noqa: F401
    except ImportError:
        return False
    return True


def _run_recipe(tmp_path, source, extra_env=None, sums=None):
    out = tmp_path / "release"
    env = {
        **os.environ,
        "PYTHON": sys.executable,
        "intermediate_output_folder": str(tmp_path / "work"),
        "should_keep_intermediate_files": "no",
        **(extra_env or {}),
    }
    args = ["bash", "recipes/paranames_content_export.sh", source, str(out), "2"]
    if sums:
        args.append(sums)
    result = subprocess.run(
        args, cwd=REPO_ROOT, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    return out, result


@pytest.mark.skipif(not _icu_available(), reason="PyICU not installed")
def test_recipe_local_parts_english(tmp_path, parts_dir):
    out, _ = _run_recipe(tmp_path, str(parts_dir))
    rows = read_tsv(out / "combined" / "paranames.tsv")
    assert {r["language"] for r in rows} == {"en"}
    assert ("Q11", "Douglas Adams") in {(r["wikidata_id"], r["eng"]) for r in rows}
    assert (out / "en" / "paranames_en.tsv").exists()
    assert (out / "extra_data" / "sitelinks.tsv").exists()
    assert not (tmp_path / "work").exists()


@pytest.mark.skipif(not _icu_available(), reason="PyICU not installed")
def test_recipe_url_base_with_checksums(tmp_path, parts_dir):
    import functools
    import http.server
    import threading

    sums = parts_dir / "SHA256SUMS"
    sums.write_text(
        "".join(
            f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n"
            for p in sorted(parts_dir.glob("*.xml.bz2"))
        )
    )
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(parts_dir)
    )
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}/"
        out, result = _run_recipe(
            tmp_path,
            base,
            extra_env={"languages": "en,de", "should_keep_intermediate_files": "yes"},
        )
    finally:
        server.shutdown()
    assert '"sha256_verified":1' in result.stderr
    rows = read_tsv(out / "combined" / "paranames.tsv")
    assert {r["language"] for r in rows} == {"en", "de"}
    # downloaded parts are deleted after extraction
    assert list((out / "intermediate" / "download").glob("*.xml.bz2")) == []
    assert len(list((out / "intermediate" / "jsonl").glob("*.jsonl.gz"))) == 2
