#!/usr/bin/env python

"""
Build the ParaNames intermediate TSV from the Wikimedia "MediaWiki Content
File Exports" of Wikidata (``*.xml.bz2`` parts), without MongoDB and without
extracting the dump to disk.

Three steps:

    extract  one XML part -> one gzip JSONL file (one line per item)
    types    all JSONL files -> P279 descendant closures of the LOC/ORG roots
    dump     all JSONL files + closures -> {PER,LOC,ORG}.tsv, combined.tsv,
             sitelinks.tsv

``combined.tsv`` has the same columns, header and quoting as the file that
``recipes/dump.sh`` gives to ``paranames/io/postprocess.py``.
"""

import bz2
import csv
import glob
import gzip
import hashlib
import os
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import IO, Dict, Iterable, Iterator, List, Optional, Set, Tuple

import click
import orjson

# Same roots as conll_type_to_wikidata_id in wikidata_dump_transliterations.py
CONLL_TYPE_TO_WIKIDATA_ID = {"PER": "Q5", "LOC": "Q82794", "ORG": "Q43229"}

# Types whose P279 descendants count (recipes/dump.sh: subclassing is off for
# PER and on for LOC and ORG by default).
SUBCLASSED_TYPES = ("LOC", "ORG")

# Column order of output_csv() in wikidata_dump_transliterations.py
TSV_FIELDS = ["wikidata_id", "name", "label", "language", "type"]

ENGLISH = "en"
MUL = "mul"
ALL_LANGUAGES = "all"

DEFAULT_EXCLUDE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "..",
    "data",
    "exclude_these_languages.txt",
)


# ---------------------------------------------------------------------------
# Language selection
# ---------------------------------------------------------------------------


def parse_language_list(value: str) -> Optional[Set[str]]:
    """``all`` -> None (every language). Otherwise a set of codes."""
    value = (value or "").strip()

    if value == ALL_LANGUAGES:
        return None

    return {code.strip() for code in value.split(",") if code.strip()}


def read_exclude_file(path: Optional[str]) -> Set[str]:
    if not path:
        return set()
    with open(path, encoding="utf-8") as fin:
        return {line.strip() for line in fin if line.strip()}


def labels_to_keep_in_extract(selected: Optional[Set[str]]) -> Optional[Set[str]]:
    """Label languages the per-part JSONL keeps. ``en`` and ``mul`` are always
    kept: they feed the ``name`` column and the entity filter."""

    if selected is None:
        return None

    return set(selected) | {ENGLISH, MUL}


# ---------------------------------------------------------------------------
# Item JSON -> compact record
# ---------------------------------------------------------------------------


def _as_dict(value) -> dict:
    # Wikibase page content serializes an empty map as [] (PHP empty array).
    return value if isinstance(value, dict) else {}


def _entity_id(datavalue_value) -> str:
    """Item id of a wikibase-entityid value.

    Raises KeyError when there is no id, like the upstream lookup
    ``iof["mainsnak"]["datavalue"]["value"]["id"]``. Older stored revisions
    can carry only ``numeric-id``; the JSON dumps that upstream reads always
    print ``id``, so the same id is rebuilt here."""

    if not isinstance(datavalue_value, dict):
        raise KeyError("id")

    if "id" in datavalue_value:
        return datavalue_value["id"]

    if datavalue_value.get("entity-type") == "item" and "numeric-id" in datavalue_value:
        return f"Q{datavalue_value['numeric-id']}"

    raise KeyError("id")


def parse_p31(claims: dict) -> List[str]:
    """P31 values as ParaNames reads them (paranames/util/wikidata.py,
    WikidataRecord.parse_instance_of): every claim of every rank, including
    deprecated ones. If one claim has no datavalue (novalue/somevalue), the
    upstream code hits a KeyError and the whole set becomes empty, so the
    entity is dropped. This is kept on purpose for fidelity."""

    try:
        return sorted(
            {_entity_id(c["mainsnak"]["datavalue"]["value"]) for c in claims["P31"]}
        )
    except (KeyError, TypeError):
        return []


def parse_p279_truthy(claims: dict) -> List[str]:
    """Truthy P279 values, like ``wdt:P279`` in WDQS: the claims of the best
    rank (preferred if any preferred claim exists, else normal; never
    deprecated). The best rank is chosen over all claims, value or not; only
    claims with ``mainsnak.datavalue.value.id`` give a value."""

    statements = claims.get("P279") or []

    if not isinstance(statements, list):
        return []

    ranks = {c.get("rank", "normal") for c in statements if isinstance(c, dict)}

    if "preferred" in ranks:
        best = "preferred"
    elif "normal" in ranks:
        best = "normal"
    else:
        return []

    values = set()

    for claim in statements:
        if not isinstance(claim, dict) or claim.get("rank", "normal") != best:
            continue
        try:
            values.add(_entity_id(claim["mainsnak"]["datavalue"]["value"]))
        except (KeyError, TypeError):
            continue

    return sorted(values)


def item_to_record(item: dict, keep_label_languages: Optional[Set[str]] = None) -> dict:
    """Compact record for the per-part JSONL.

    Labels and sitelinks are kept only for candidate items (an ``en`` or
    ``mul`` label and a non-empty P31). The other items can never be output;
    they keep ``id``, ``p31`` and ``p279`` because P279 builds the closures."""

    claims = _as_dict(item.get("claims"))
    p31 = parse_p31(claims)
    p279 = parse_p279_truthy(claims)
    raw_labels = _as_dict(item.get("labels"))

    labels: Dict[str, str] = {}
    sitelinks: Dict[str, str] = {}
    is_candidate = bool(p31) and (ENGLISH in raw_labels or MUL in raw_labels)

    if is_candidate:
        for lang, d in raw_labels.items():
            if keep_label_languages is not None and lang not in keep_label_languages:
                continue
            labels[lang] = d["value"] if isinstance(d, dict) else d

        for site, d in _as_dict(item.get("sitelinks")).items():
            sitelinks[site] = d.get("title", "") if isinstance(d, dict) else ""

    return {
        "id": item["id"],
        "p31": p31,
        "p279": p279,
        "labels": labels,
        "sitelinks": sitelinks,
    }


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------


class HashingReader:
    """File wrapper that feeds every byte read into a hash."""

    def __init__(self, raw: IO[bytes]) -> None:
        self.raw = raw
        self.hash = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        data = self.raw.read(size)
        self.hash.update(data)
        return data

    def readinto(self, buffer) -> int:
        data = self.read(len(buffer))
        n = len(data)
        buffer[:n] = data
        return n

    def readable(self) -> bool:
        return True

    def drain(self) -> None:
        while self.read(1 << 20):
            pass

    def close(self) -> None:
        self.raw.close()


class ChecksumError(Exception):
    pass


def expected_sha256(sums_file: str, part_path: str) -> str:
    name = os.path.basename(part_path)
    with open(sums_file, encoding="utf-8") as fin:
        for line in fin:
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[1].lstrip("*") == name:
                return parts[0].lower()
    raise ChecksumError(f"{name} is not listed in {sums_file}")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(elem, name: str):
    for c in elem:
        if _local(c.tag) == name:
            return c
    return None


def iter_pages(
    stream: IO[bytes],
) -> Iterator[Tuple[Optional[str], Optional[str], Optional[str], Optional[str], bool]]:
    """Yield (title, ns, model, text, is_redirect) per <page>, clearing the
    tree as it goes. Tags are matched by local name, so any export schema
    version works."""

    context = ET.iterparse(stream, events=("start", "end"))
    root = None

    for event, elem in context:
        if event == "start":
            if root is None:
                root = elem
            continue

        if _local(elem.tag) != "page":
            continue

        title_el = _child(elem, "title")
        ns_el = _child(elem, "ns")
        redirect = _child(elem, "redirect") is not None
        revision = _child(elem, "revision")
        model = text = None

        if revision is not None:
            model_el = _child(revision, "model")
            text_el = _child(revision, "text")
            model = model_el.text if model_el is not None else None
            text = text_el.text if text_el is not None else None

        yield (
            title_el.text if title_el is not None else None,
            ns_el.text.strip() if ns_el is not None and ns_el.text else None,
            model,
            text,
            redirect,
        )

        elem.clear()
        if root is not None:
            root.clear()


def extract_part(
    part_path: str,
    output_path: str,
    sha256sums: Optional[str] = None,
    languages: str = ENGLISH,
) -> Counter:
    """Stream one ``.xml.bz2`` part into ``output_path`` (gzip JSONL).

    The output is written to a temporary file and renamed at the end, so an
    existing output file is always complete."""

    keep = labels_to_keep_in_extract(parse_language_list(languages))
    expected = expected_sha256(sha256sums, part_path) if sha256sums else None
    stats: Counter = Counter()
    tmp_path = output_path + ".tmp"

    raw = open(part_path, "rb")
    reader = HashingReader(raw)

    try:
        with bz2.open(reader, "rb") as xml_stream, gzip.open(tmp_path, "wb") as fout:
            for title, ns, model, text, redirect in iter_pages(xml_stream):
                stats["pages_seen"] += 1

                if ns != "0":
                    stats["skipped_namespace"] += 1
                    continue
                if model != "wikibase-item":
                    stats["skipped_model"] += 1
                    continue

                stats["item_pages"] += 1

                if redirect:
                    stats["skipped_redirect"] += 1
                    continue
                if not text:
                    stats["skipped_no_text"] += 1
                    continue
                try:
                    item = orjson.loads(text)
                except orjson.JSONDecodeError:
                    stats["skipped_json_error"] += 1
                    continue
                if not isinstance(item, dict) or "redirect" in item:
                    stats["skipped_redirect"] += 1
                    continue
                if item.get("type") != "item" or "id" not in item:
                    stats["skipped_not_item"] += 1
                    continue

                record = item_to_record(item, keep)
                fout.write(orjson.dumps(record) + b"\n")
                stats["items_written"] += 1
                if record["labels"]:
                    stats["candidate_items"] += 1

        reader.drain()

        if expected is not None:
            actual = reader.hash.hexdigest()
            if actual != expected:
                raise ChecksumError(
                    f"SHA-256 mismatch for {part_path}: expected {expected}, got {actual}"
                )
            stats["sha256_verified"] = 1

        os.replace(tmp_path, output_path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    finally:
        reader.close()

    return stats


# ---------------------------------------------------------------------------
# types
# ---------------------------------------------------------------------------


def iter_records(input_dir: str) -> Iterator[dict]:
    files = sorted(glob.glob(os.path.join(input_dir, "*.jsonl.gz")))
    if not files:
        raise click.ClickException(f"No *.jsonl.gz files in {input_dir}")
    for path in files:
        with gzip.open(path, "rb") as fin:
            for line in fin:
                if line.strip():
                    yield orjson.loads(line)


def descendant_closure(children: Dict[str, Set[str]], root: str) -> Set[str]:
    """Root plus every P279 descendant (``wdt:P279*``). BFS; cycles are safe."""

    seen = {root}
    queue = deque([root])

    while queue:
        node = queue.popleft()
        for child in children.get(node, ()):
            if child not in seen:
                seen.add(child)
                queue.append(child)

    return seen


def closure_path(closures_dir: str, conll_type: str) -> str:
    return os.path.join(
        closures_dir,
        f"{conll_type}_{CONLL_TYPE_TO_WIKIDATA_ID[conll_type]}_closure.txt",
    )


def build_closures(input_dir: str, output_dir: str) -> Dict[str, Set[str]]:
    children: Dict[str, Set[str]] = defaultdict(set)

    for record in iter_records(input_dir):
        for parent in record.get("p279", ()):
            children[parent].add(record["id"])

    os.makedirs(output_dir, exist_ok=True)
    closures = {}

    for conll_type in SUBCLASSED_TYPES:
        closure = descendant_closure(children, CONLL_TYPE_TO_WIKIDATA_ID[conll_type])
        closures[conll_type] = closure
        with open(closure_path(output_dir, conll_type), "w", encoding="utf-8") as fout:
            for qid in sorted(closure, key=lambda q: (len(q), q)):
                fout.write(qid + "\n")

    return closures


def read_closures(closures_dir: str) -> Dict[str, Set[str]]:
    closures = {}
    for conll_type in SUBCLASSED_TYPES:
        with open(closure_path(closures_dir, conll_type), encoding="utf-8") as fin:
            closures[conll_type] = {line.strip() for line in fin if line.strip()}
    return closures


# ---------------------------------------------------------------------------
# dump
# ---------------------------------------------------------------------------


def assign_types(p31: Iterable[str], closures: Dict[str, Set[str]]) -> List[str]:
    p31_set = set(p31)
    types = []
    if CONLL_TYPE_TO_WIKIDATA_ID["PER"] in p31_set:
        types.append("PER")
    for conll_type in SUBCLASSED_TYPES:
        if p31_set & closures[conll_type]:
            types.append(conll_type)
    return types


def english_name(labels: Dict[str, str], mul_fallback: Set[str]) -> Optional[str]:
    """Value of the ``name`` column (renamed ``eng`` by postprocess.py), or
    None when the entity has no usable English label."""

    if ENGLISH in labels:
        return labels[ENGLISH]
    if ENGLISH in mul_fallback and MUL in labels:
        return labels[MUL]
    return None


def label_rows(
    labels: Dict[str, str],
    selected: Optional[Set[str]],
    excluded: Set[str],
    mul_fallback: Set[str],
) -> List[Tuple[str, str]]:
    """(language, label) pairs to output for one entity.

    A ``mul`` label is never output as its own language. For every language
    in ``mul_fallback`` that the entity lacks, the ``mul`` value is output in
    that language instead (at the position of the ``mul`` label)."""

    def wanted(lang: str) -> bool:
        if lang in excluded:
            return False
        return selected is None or lang in selected

    rows = []
    for lang, label in labels.items():
        if lang == MUL:
            for fb_lang in sorted(mul_fallback):
                if fb_lang not in labels and wanted(fb_lang):
                    rows.append((fb_lang, label))
            continue
        if wanted(lang):
            rows.append((lang, label))
    return rows


class TypeWriter:
    """One per-type TSV, written like output_csv() in
    wikidata_dump_transliterations.py (csv.DictWriter, excel dialect with a
    tab delimiter)."""

    def __init__(self, path: str) -> None:
        self.f = open(path, "w", encoding="utf-8", newline="")
        self.writer = csv.DictWriter(
            self.f, delimiter="\t", fieldnames=TSV_FIELDS, extrasaction="ignore"
        )
        self.writer.writeheader()

    def write(self, row: dict) -> None:
        self.writer.writerow(row)

    def close(self) -> None:
        self.f.close()


def dump_tsv(
    input_dir: str,
    closures_dir: str,
    output_dir: str,
    languages: str = ENGLISH,
    exclude_file: Optional[str] = DEFAULT_EXCLUDE_FILE,
    mul_fallback: str = ENGLISH,
) -> Counter:
    selected = parse_language_list(languages)
    excluded = read_exclude_file(exclude_file)
    fallback = parse_language_list(mul_fallback) or set()
    closures = read_closures(closures_dir)
    os.makedirs(output_dir, exist_ok=True)

    writers = {
        t: TypeWriter(os.path.join(output_dir, f"{t}.tsv"))
        for t in CONLL_TYPE_TO_WIKIDATA_ID
    }
    sitelinks_f = open(
        os.path.join(output_dir, "sitelinks.tsv"), "w", encoding="utf-8", newline=""
    )
    sitelinks_w = csv.writer(sitelinks_f, delimiter="\t", lineterminator="\n")
    sitelinks_w.writerow(["wikidata_id", "site"])
    stats: Counter = Counter()

    try:
        for record in iter_records(input_dir):
            labels = record.get("labels") or {}
            if not labels:
                continue
            types = assign_types(record.get("p31", ()), closures)
            if not types:
                continue
            name = english_name(labels, fallback)
            if name is None:
                stats["dropped_no_english"] += 1
                continue
            if ENGLISH not in labels:
                stats["mul_as_english"] += 1

            rows = label_rows(labels, selected, excluded, fallback)
            stats["typed_entities"] += 1
            for conll_type in types:
                stats[f"entities_{conll_type}"] += 1
                for lang, label in rows:
                    writers[conll_type].write(
                        {
                            "wikidata_id": record["id"],
                            "name": name,
                            "label": label,
                            "language": lang,
                            "type": conll_type,
                        }
                    )
                    stats[f"rows_{conll_type}"] += 1
            for site in record.get("sitelinks") or {}:
                sitelinks_w.writerow([record["id"], site])
                stats["sitelinks"] += 1
    finally:
        for w in writers.values():
            w.close()
        sitelinks_f.close()

    combine_type_files(output_dir, os.path.join(output_dir, "combined.tsv"))
    return stats


def combine_type_files(type_dir: str, output_path: str) -> None:
    """Same result as ``xsv cat rows -d"\\t" LOC.tsv ORG.tsv PER.tsv |
    xsv fmt -t"\\t"`` in recipes/dump.sh: one header, the files in glob
    (alphabetical) order, LF line endings, minimal quoting."""

    with open(output_path, "w", encoding="utf-8", newline="") as fout:
        writer = csv.writer(fout, delimiter="\t", lineterminator="\n")
        writer.writerow(TSV_FIELDS)
        for conll_type in sorted(CONLL_TYPE_TO_WIKIDATA_ID):
            with open(
                os.path.join(type_dir, f"{conll_type}.tsv"),
                encoding="utf-8",
                newline="",
            ) as fin:
                reader = csv.reader(fin, delimiter="\t")
                next(reader, None)
                for row in reader:
                    writer.writerow(row)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _log_stats(step: str, stats: Counter) -> None:
    print(
        f"[{step}] " + orjson.dumps(dict(sorted(stats.items()))).decode(),
        file=sys.stderr,
    )


languages_option = click.option(
    "--languages",
    "-l",
    default=ENGLISH,
    show_default=True,
    help="Comma-separated label languages to output, or 'all' "
    "(every language except the exclude file).",
)


@click.group()
def cli() -> None:
    """ParaNames from the Wikidata content export (no MongoDB)."""


@cli.command()
@click.option(
    "--input-file", "-i", required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option("--output-file", "-o", required=True, help="Output .jsonl.gz")
@click.option(
    "--sha256sums",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="SHA256SUMS file to verify the part against",
)
@languages_option
def extract(input_file, output_file, sha256sums, languages) -> None:
    """Stream one .xml.bz2 part into a gzip JSONL file."""
    try:
        stats = extract_part(input_file, output_file, sha256sums, languages)
    except ChecksumError as e:
        raise click.ClickException(str(e))
    _log_stats(f"extract {os.path.basename(input_file)}", stats)


@cli.command()
@click.option(
    "--input-dir", "-i", required=True, type=click.Path(exists=True, file_okay=False)
)
@click.option("--output-dir", "-o", required=True)
def types(input_dir, output_dir) -> None:
    """Compute the P279 descendant closures of the LOC and ORG roots."""
    closures = build_closures(input_dir, output_dir)
    _log_stats("types", Counter({f"closure_{t}": len(c) for t, c in closures.items()}))


@cli.command()
@click.option(
    "--input-dir", "-i", required=True, type=click.Path(exists=True, file_okay=False)
)
@click.option(
    "--closures-dir", "-c", required=True, type=click.Path(exists=True, file_okay=False)
)
@click.option("--output-dir", "-o", required=True)
@languages_option
@click.option(
    "--exclude-languages-file",
    default=DEFAULT_EXCLUDE_FILE,
    show_default=True,
    help="One language code per line; never output",
)
@click.option(
    "--mul-fallback",
    default=ENGLISH,
    show_default=True,
    help="Comma-separated languages for which a 'mul' label is used when the language's own label is missing ('' for none)",
)
def dump(
    input_dir, closures_dir, output_dir, languages, exclude_languages_file, mul_fallback
) -> None:
    """Write {PER,LOC,ORG}.tsv, combined.tsv and sitelinks.tsv."""
    stats = dump_tsv(
        input_dir,
        closures_dir,
        output_dir,
        languages,
        exclude_languages_file,
        mul_fallback,
    )
    _log_stats("dump", stats)


if __name__ == "__main__":
    cli()
