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


# Id prefix per Wikibase entity type, to rebuild an id from ``numeric-id``.
ENTITY_TYPE_PREFIX = {"item": "Q", "property": "P", "lexeme": "L"}


class UnresolvedId(Exception):
    """A datavalue is present but no entity id can be read or rebuilt."""


def _entity_id(datavalue_value) -> str:
    """Entity id of a wikibase-entityid value.

    Older stored revisions can carry only ``numeric-id`` and ``entity-type``;
    the JSON dumps that upstream reads always print ``id``, so the same id is
    rebuilt here. Raises UnresolvedId when neither works."""

    if isinstance(datavalue_value, dict):
        if "id" in datavalue_value:
            return datavalue_value["id"]

        prefix = ENTITY_TYPE_PREFIX.get(datavalue_value.get("entity-type"))
        if prefix and "numeric-id" in datavalue_value:
            return f"{prefix}{datavalue_value['numeric-id']}"

    raise UnresolvedId(repr(datavalue_value)[:200])


def parse_p31(claims: dict, stats: Optional[Counter] = None) -> List[str]:
    """P31 values as ParaNames reads them (paranames/util/wikidata.py,
    WikidataRecord.parse_instance_of): every claim of every rank, including
    deprecated ones. If one claim has no datavalue (novalue/somevalue), the
    upstream code hits a KeyError and the whole set becomes empty, so the
    entity is dropped. This is kept on purpose for fidelity.

    A value whose id cannot be read or rebuilt is left out and counted in
    ``stats["p31_values_unresolved"]``; the other values are kept."""

    statements = claims.get("P31")
    if not isinstance(statements, list):
        return []

    values = set()
    for c in statements:
        try:
            value = c["mainsnak"]["datavalue"]["value"]
        except (KeyError, TypeError):
            return []
        try:
            values.add(_entity_id(value))
        except UnresolvedId:
            if stats is not None:
                stats["p31_values_unresolved"] += 1
    return sorted(values)


def parse_p279_truthy(claims: dict, stats: Optional[Counter] = None) -> List[str]:
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
            value = claim["mainsnak"]["datavalue"]["value"]
        except (KeyError, TypeError):
            continue
        try:
            values.add(_entity_id(value))
        except UnresolvedId:
            if stats is not None:
                stats["p279_values_unresolved"] += 1

    return sorted(values)


def item_to_record(
    item: dict,
    keep_label_languages: Optional[Set[str]] = None,
    stats: Optional[Counter] = None,
) -> Optional[dict]:
    """Compact record for the per-part JSONL, or None for an unusable item.

    Labels and sitelinks are kept only for candidate items (an ``en`` or
    ``mul`` label and a non-empty P31). A non-candidate item with P279 edges
    is kept with empty labels, because P279 builds the class closures. A
    non-candidate item without P279 edges can neither be output nor shape the
    closures, so it is skipped (None)."""

    claims = _as_dict(item.get("claims"))
    p31 = parse_p31(claims, stats)
    p279 = parse_p279_truthy(claims, stats)
    raw_labels = _as_dict(item.get("labels"))

    labels: Dict[str, str] = {}
    sitelinks: Dict[str, str] = {}
    is_candidate = bool(p31) and (ENGLISH in raw_labels or MUL in raw_labels)

    if not is_candidate and not p279:
        return None

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


class ExtractError(Exception):
    pass


# ---------------------------------------------------------------------------
# Sidecar with the extract settings (``<output>.meta.json``)
# ---------------------------------------------------------------------------

META_FORMAT_VERSION = 2


def language_key(languages: str) -> str:
    """Normal form of a language selection: ``all`` or sorted codes."""
    selected = parse_language_list(languages)
    return ALL_LANGUAGES if selected is None else ",".join(sorted(selected))


def meta_path(output_path: str) -> str:
    return output_path + ".meta.json"


def read_meta(output_path: str) -> Optional[dict]:
    try:
        with open(meta_path(output_path), "rb") as fin:
            return orjson.loads(fin.read())
    except (OSError, orjson.JSONDecodeError):
        return None


def part_fingerprint(part_path: str) -> Dict[str, int]:
    st = os.stat(part_path)
    return {"part_size": st.st_size, "part_mtime_ns": st.st_mtime_ns}


def extract_is_current(
    output_path: str,
    languages: str,
    part_path: Optional[str] = None,
    sha256sums: Optional[str] = None,
) -> bool:
    """True when ``output_path`` exists, was extracted with the same language
    selection, and comes from the same part.

    The part is identified by its SHA-256 when ``sha256sums`` is given (the
    recorded hash must equal the listed one; the part file need not exist, so
    a URL run can check before downloading), else by the size and mtime of
    ``part_path``. With neither, the part is not checked.

    The ``mul`` fallback is applied in ``dump`` only; ``extract`` always keeps
    ``en`` and ``mul``, so it does not matter here."""
    meta = read_meta(output_path)
    if not (
        os.path.exists(output_path)
        and meta is not None
        and meta.get("format_version") == META_FORMAT_VERSION
        and meta.get("languages") == language_key(languages)
    ):
        return False

    if sha256sums:
        name = meta.get("part")
        if part_path and os.path.basename(part_path) != name:
            return False
        try:
            return meta.get("sha256") == expected_sha256(sha256sums, name)
        except ChecksumError:
            return False

    if part_path:
        if not os.path.exists(part_path):
            return False
        fp = part_fingerprint(part_path)
        return all(meta.get(k) == v for k, v in fp.items())

    return True


def selection_covers(extracted: str, requested: str) -> bool:
    have = parse_language_list(extracted)
    want = parse_language_list(requested)
    if have is None:
        return True
    if want is None:
        return False
    return want <= have


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


def _remove_if_exists(*paths: str) -> None:
    for path in paths:
        if os.path.exists(path):
            os.remove(path)


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
    for path in (output_path, meta_path(output_path)):
        if os.path.exists(path):
            os.remove(path)
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
                except orjson.JSONDecodeError as e:
                    raise ExtractError(
                        f"{part_path}: invalid JSON in page {title!r}: {e}"
                    )
                if not isinstance(item, dict) or "redirect" in item:
                    stats["skipped_redirect"] += 1
                    continue
                if item.get("type") != "item" or "id" not in item:
                    stats["skipped_not_item"] += 1
                    continue

                stats["items_parsed"] += 1
                record = item_to_record(item, keep, stats)
                if record is None:
                    stats["skipped_unusable"] += 1
                    continue
                fout.write(orjson.dumps(record) + b"\n")
                stats["items_written"] += 1
                if record["labels"]:
                    stats["candidate_items"] += 1

        reader.drain()

        if stats["pages_seen"] == 0:
            raise ExtractError(f"{part_path}: no <page> elements")
        if stats["item_pages"] == 0:
            print(
                f"[extract {os.path.basename(part_path)}] WARNING: no item pages "
                f"({stats['pages_seen']} pages seen)",
                file=sys.stderr,
            )
        # Redirect item pages count as read: a part may hold only redirects.
        elif stats["items_parsed"] + stats["skipped_redirect"] == 0:
            raise ExtractError(
                f"{part_path}: {stats['item_pages']} item pages but no item could be read"
            )

        if expected is not None:
            actual = reader.hash.hexdigest()
            if actual != expected:
                raise ChecksumError(
                    f"SHA-256 mismatch for {part_path}: expected {expected}, got {actual}"
                )
            stats["sha256_verified"] = 1

        meta = {
            "format_version": META_FORMAT_VERSION,
            "languages": language_key(languages),
            "part": os.path.basename(part_path),
            "sha256": reader.hash.hexdigest(),
            **part_fingerprint(part_path),
            "stats": dict(stats),
        }
        with open(meta_path(output_path), "wb") as fmeta:
            fmeta.write(orjson.dumps(meta))
        os.replace(tmp_path, output_path)
    except (OSError, EOFError, ET.ParseError) as e:
        _remove_if_exists(tmp_path, meta_path(output_path))
        raise ExtractError(f"{part_path}: extract failed: {e}") from e
    except BaseException:
        _remove_if_exists(tmp_path, meta_path(output_path))
        raise
    finally:
        reader.close()

    return stats


# ---------------------------------------------------------------------------
# types
# ---------------------------------------------------------------------------


def jsonl_files(input_dir: str) -> List[str]:
    files = sorted(glob.glob(os.path.join(input_dir, "*.jsonl.gz")))
    if not files:
        raise click.ClickException(f"No *.jsonl.gz files in {input_dir}")
    return files


def check_extract_languages(input_dir: str, languages: str) -> None:
    """Fail when a JSONL file was extracted with a selection that does not
    cover ``languages`` (its labels would be missing from the output)."""
    for path in jsonl_files(input_dir):
        meta = read_meta(path)
        if meta is None:
            raise click.ClickException(
                f"{path}: no {meta_path(path)}; run extract again"
            )
        if not selection_covers(meta.get("languages", ""), languages):
            raise click.ClickException(
                f"{path} was extracted with --languages {meta.get('languages')}, "
                f"which does not cover --languages {languages}; run extract again"
            )


def iter_records(input_dir: str) -> Iterator[dict]:
    for path in jsonl_files(input_dir):
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
    fallback = parse_language_list(mul_fallback)
    if fallback is None:
        raise click.BadParameter(
            "'all' is not allowed; give a comma-separated list of languages, or '' for none",
            param_hint="--mul-fallback",
        )
    check_extract_languages(input_dir, languages)
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

    if stats["typed_entities"] == 0:
        raise click.ClickException(
            f"No typed entities in {input_dir} (languages={languages}, "
            f"mul_fallback={mul_fallback!r}); the TSV would be empty. "
            "Check the parts, the closures and the language selection."
        )

    combine_type_files(output_dir, os.path.join(output_dir, "combined.tsv"))
    return stats


def combine_type_files(type_dir: str, output_path: str) -> None:
    """Stands in for ``xsv cat rows -d"\\t" LOC.tsv ORG.tsv PER.tsv |
    xsv fmt -t"\\t"`` in recipes/dump.sh: one header, the files in glob
    (alphabetical) order, LF line endings, Python csv minimal quoting.
    The output was not compared byte for byte with real xsv output."""

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
@click.option(
    "--resume",
    is_flag=True,
    help="Skip the part when its output exists with the same language selection",
)
def extract(input_file, output_file, sha256sums, languages, resume) -> None:
    """Stream one .xml.bz2 part into a gzip JSONL file."""
    if resume and extract_is_current(output_file, languages, input_file, sha256sums):
        print(
            f"[extract {os.path.basename(input_file)}] up to date, skipped",
            file=sys.stderr,
        )
        return
    try:
        stats = extract_part(input_file, output_file, sha256sums, languages)
    except (ChecksumError, ExtractError) as e:
        raise click.ClickException(str(e))
    _log_stats(f"extract {os.path.basename(input_file)}", stats)


@cli.command("is-current")
@click.option("--output-file", "-o", required=True)
@click.option(
    "--input-file", "-i", default=None, help="Local part (size and mtime are compared)"
)
@click.option(
    "--sha256sums", default=None, help="SHA256SUMS file (the listed hash is compared)"
)
@languages_option
def is_current(output_file, input_file, sha256sums, languages) -> None:
    """Exit 0 when OUTPUT_FILE exists, was extracted with the same language
    selection and from the same part, else exit 1."""
    ok = extract_is_current(output_file, languages, input_file, sha256sums)
    sys.exit(0 if ok else 1)


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
