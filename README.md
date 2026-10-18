<img src="data/paranames_banner.png"></img>

# ParaNames 1.0: Creating an Entity Name Corpus for 400+ Languages using Wikidata

This repository is the home of the ParaNames project, a massively multilingual dataset consisting of parallel names of over 16.8 million named entities in over 400 languages. This README contains links to corpus releases as well as code used in our canonical name translation and named entity recognition experiments.

ParaNames was originally introduced in [Sälevä, J. and Lignos, C., 2022. ParaNames: A Massively Multilingual Entity Name Corpus. arXiv preprint arXiv:2202.14035](https://arxiv.org/abs/2202.14035) and subsequently published at LREC-COLING 2024.

Please cite as:
```
@inproceedings{saleva-lignos-2024-paranames-1,
    title = "{P}ara{N}ames 1.0: Creating an Entity Name Corpus for 400+ Languages Using {W}ikidata",
    author = {S{\"a}lev{\"a}, Jonne  and
      Lignos, Constantine},
    editor = "Calzolari, Nicoletta  and
      Kan, Min-Yen  and
      Hoste, Veronique  and
      Lenci, Alessandro  and
      Sakti, Sakriani  and
      Xue, Nianwen",
    booktitle = "Proceedings of the 2024 Joint International Conference on Computational Linguistics, Language Resources and Evaluation (LREC-COLING 2024)",
    month = may,
    year = "2024",
    address = "Torino, Italy",
    publisher = "ELRA and ICCL",
    url = "https://aclanthology.org/2024.lrec-main.1103",
    pages = "12599--12610",
    abstract = "We introduce ParaNames, a massively multilingual parallel name resource consisting of 140 million names spanning over 400 languages. Names are provided for 16.8 million entities, and each entity is mapped from a complex type hierarchy to a standard type (PER/LOC/ORG). Using Wikidata as a source, we create the largest resource of this type to date. We describe our approach to filtering and standardizing the data to provide the best quality possible. ParaNames is useful for multilingual language processing, both in defining tasks for name translation/transliteration and as supplementary data for tasks such as named entity recognition and linking. We demonstrate the usefulness of ParaNames on two tasks. First, we perform canonical name translation between English and 17 other languages. Second, we use it as a gazetteer for multilingual named entity recognition, obtaining performance improvements on all 10 languages evaluated.",
}
```

While we prefer you cite the LREC-COLING version above, the Arxiv preprint can be cited as

```bibtex
@misc{sälevä2024paranames,
      title={ParaNames 1.0: Creating an Entity Name Corpus for 400+ Languages using Wikidata},
      author={Jonne Sälevä and Constantine Lignos},
      year={2024},
      eprint={2405.09496},
      archivePrefix={arXiv},
      primaryClass={cs.CL}
}
```

See the [Releases page](https://github.com/bltlab/paranames/releases) for the downloadable release.

## Experimental results from paper

**NOTE**: The code for the downstream experiments does NOT live in this repository. See below for links.

### Canonical name translation

[Link to repository](https://github.com/j0ma/paranames-canonical-name-translation)

This repository contains the code for running our canonical name translation experiments with `fairseq`.

### Named entity recognition

[Link to repository](https://github.com/j0ma/paranames-named-entity-recognition)

This repository contains the code for running our canonical name translation experiments with `dynet`.

# Using the data release

## Format

The corpus is released as a gzipped TSV file which contains the following columns:

* `wikidata_id`: the Wikidata ID of the entity
* `eng`: the English name of the entity
* `label`: the name of the entity in the language of the row
* `language`: the language of the row
* `type`: the type of the entity (PER, LOC, ORG)

Some example rows are shown below:
```
wikidata_id     eng     label   language        type
Q181893 Fredericksburg and Spotsylvania National Military Park  Fredericksburg and Spotsylvania National Military Park  mg      LOC
Q257160 Burgheim        Burgheim        fr      LOC
Q508851 Triefenstein    Triefenstein    nl      LOC
Q303923 Ruhstorf an der Rott    Ruhstorf an der Rott    bar     LOC
Q284696 Oberelsbach     Oberelsbach     wo      LOC
Q550561 Triftern        Թրիֆթերն        hy      LOC
Q529488 Reisbach        Reisbach        fr      LOC
Q385427 Stadtlauringen  Stadtlauringen  ia      LOC
Q505327 Wildflecken     Wildflecken     id      LOC
Q505288 Ipsheim Իպսհայմ hy      LOC
```


## Notes

### Repeated entities

In current releases, any entity that is associated with multiple named entity types (PER, LOC, ORG) in the Wikidata type hierarchy will appear multiple times in the output, once with each type. This affects less than 3% of the entities in the data.

If you want a unique set of entities, you should deduplicate the data using the `wikidata_id` field.

If you only want to use entities that are associated with a single named entity type, you should remove any `wikidata_id` that appears in multiple rows.


# Using the code

First, install the following non-Python dependencies:

- MongoDB
- [xsv](https://github.com/BurntSushi/xsv)
- ICU support for your computer (e.g. `libicu-dev`)

Next, install ParaNames and its Python dependencies by running `pip install -e .`.

It is recommended that you use a Conda environment for package management.

## Creating the ParaNames corpus

To create a corpus following our approach, follow the steps below:

1. Download the latest Wikidata dump from the [Wikimedia page](https://dumps.wikimedia.org/wikidatawiki/entities/) and extract it. Note that this may take up several TB of disk space.
2. Use `recipes/paranames_pipeline.sh` which ingests the Wikidata JSON to MongoDB and then dumps and postprocesses it to our final TSV resource.

The call to `recipes/paranames_pipeline.sh` works as follows:

```
recipes/paranames_pipeline.sh <path_to_extracted_json_dump> <output_folder> <n_workers>
```

Set the number of workers based on the number of CPUs your machine has.
By default, only 1 CPU is used.

The output folder will contain one subfolder per language, inside of which `paranames_<language_code>.tsv` can be found.
The entire resource is located in `<output_folder>/combined/paranames.tsv`.

### Notes


ParaNames offers several options for customization:

- If your MongoDB instance uses a non-standard port, you should change the value of [`mongodb_port`](https://github.com/bltlab/paranames/blob/main/recipes/paranames_pipeline.sh#L13) accordingly inside `paranames_pipeline.sh`.

- Setting [`should_collapse_languages=yes`](https://github.com/bltlab/paranames/blob/main/recipes/dump.sh#L17) will cause Wikimedia language codes to be "collapsed" to the top-level Wikimedia language code, i.e. `kk-cyrl` will be converted to `kk`, `en-ca` to `en` etc.

- Setting [`should_keep_intermediate_files=yes`](https://github.com/bltlab/paranames/blob/main/recipes/dump.sh#L18) will cause intermediate files to be deleted. This includes the raw per-type TSV dumps (`{PER,LOC,ORG}.tsv`) from MongoDB, as well as outputs of `postprocess.py`.

- Within [`recipes/dump.sh`](https://github.com/bltlab/paranames/blob/main/recipes/dump.sh), it is also possible to define languages to be excluded and whether entity types should be disambiguated. By default, no languages are excluded and no disambiguation is done.

- After the pipeline completes, `<output_folder>` will contain one folder per language, inside of which is a TSV file containing the subset of names in that language. Combined TSVs with names in all languages are available in the `combined` folder.

## Rebuild from the Wikidata content export (no MongoDB)

This fork adds a second way to build the corpus. It reads the monthly Wikimedia
"MediaWiki Content File Exports" of Wikidata
(`https://dumps.wikimedia.org/other/mediawiki_content_current/wikidatawiki/<YYYY-MM-DD>/xml/bzip2/`,
a set of `*.xml.bz2` parts and a `SHA256SUMS` file). Each part is streamed: it is
never extracted to disk, and MongoDB is not used. The MongoDB pipeline above is
unchanged.

### Commands

Run from the repository root (with `pip install -e .` done, and ICU for
`script_standardization.py`; `xsv` is not needed for this path):

```
bash recipes/paranames_content_export.sh <parts_dir_or_url_base> <output_folder> <n_workers> [sha256sums]
```

- `<parts_dir_or_url_base>` is a local folder with the `*.xml.bz2` parts, or the
  URL of the `xml/bzip2/` folder. With a URL, each worker downloads one part,
  extracts it and deletes it. For a URL, `sha256sums` defaults to
  `<url_base>/SHA256SUMS`; every part is checked against it.
- A part whose JSONL file already exists is skipped, so an interrupted run can be
  resumed with the same `intermediate_output_folder`.
- Environment variables: `languages` (default `en`), `mul_fallback` (default `en`),
  `should_keep_intermediate_files` (default `no`), `intermediate_output_folder`,
  `should_disambiguate_types` and `should_collapse_languages` (default `no`, as in
  `recipes/dump.sh`), `PYTHON`.

The steps are also available one by one in
`paranames/io/wikidata_content_export.py`:

```
python paranames/io/wikidata_content_export.py extract -i <part.xml.bz2> -o <part.jsonl.gz> [--sha256sums SHA256SUMS] [--languages en]
python paranames/io/wikidata_content_export.py types -i <jsonl_folder> -o <closures_folder>
python paranames/io/wikidata_content_export.py dump -i <jsonl_folder> -c <closures_folder> -o <tsv_folder> [--languages en] [--mul-fallback en]
```

`dump` writes `PER.tsv`, `LOC.tsv`, `ORG.tsv` and `combined.tsv` (the same
columns, header and quoting as the file that `recipes/dump.sh` gives to
`postprocess.py`), and `sitelinks.tsv` (`wikidata_id`, `site`) for the typed
entities. After that, the recipe runs `postprocess.py`,
`script_standardization.py` and `separate_by_language.py` with the same
arguments as `recipes/dump.sh`. The closures and `sitelinks.tsv` are copied to
`<output_folder>/extra_data/`.

### Languages

The default run is English only (`languages=en`). To request more languages,
give a comma-separated list of Wikimedia language codes, or `all` for every
language except the codes in `data/exclude_these_languages.txt` (as upstream):

```
languages=en,de,fr bash recipes/paranames_content_export.sh ...
languages=all bash recipes/paranames_content_export.sh ...
```

The selection is applied in `extract` already, so the intermediate JSONL files
keep only the selected labels plus `en` and `mul`. A JSONL set made with a
smaller selection cannot be dumped with a larger one; run `extract` again.

### Differences from ParaNames v2024.05.07.0

- **`mul` labels.** Upstream requires an `en` label. Here, an entity with no
  `en` label but a `mul` label uses the `mul` value as its English name (the
  `eng` column) and gets an `en` row with that value. A `mul` label is never
  output as a language row of its own. This is controlled by `mul_fallback`
  (`--mul-fallback`, default `en`; an empty value turns it off). Other languages
  can be added to that list, but no other language uses `mul` by default.
- **Subclass closure from the dump.** Upstream gets the subclasses of LOC
  (`Q82794`) and ORG (`Q43229`) from a live WDQS query (`wdt:P279*`). Here they
  are computed from the truthy P279 statements in the same export (preferred
  rank if any preferred statement exists, else normal; never deprecated), so the
  types match the dump date. PER is still `Q5` only.
- **Entity filter with a language list.** Upstream, a run with a language list
  selects entities that have a label in one of those languages. Here, every
  entity needs an `en` label (or a `mul` label, see above), whatever the
  selection.

### License

The readme of the content export says that its textual data is licensed under
CC BY-SA 4.0. Wikidata's structured data is released under CC0. Both statements
are given here; check which one applies to your use.

### Resources

Disk: about one part per worker (for a URL base), plus the per-part JSONL files
and the TSV outputs. No full run has been measured yet.
