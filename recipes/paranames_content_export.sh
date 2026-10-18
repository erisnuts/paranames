#!/usr/bin/env bash

# Rebuild ParaNames from the Wikidata "MediaWiki Content File Exports"
# (https://dumps.wikimedia.org/other/mediawiki_content_current/wikidatawiki/<date>/xml/bzip2/)
# without MongoDB and without extracting the dump to disk.
#
# Run from the repository root.

set -euo pipefail

usage () {
    echo "Usage: bash recipes/paranames_content_export.sh PARTS_DIR_OR_URL_BASE OUTPUT_FOLDER [N_WORKERS=1] [SHA256SUMS]"
    echo
    echo "  PARTS_DIR_OR_URL_BASE  local folder with *.xml.bz2 parts, or the http(s) URL of the"
    echo "                         xml/bzip2/ folder (parts are downloaded one per worker and"
    echo "                         deleted after extraction)"
    echo "  SHA256SUMS             local path or URL; for a URL base, defaults to <URL_BASE>/SHA256SUMS"
    echo
    echo "Environment: languages=en (comma-separated or 'all'), mul_fallback=en,"
    echo "             should_keep_intermediate_files=no,"
    echo "             intermediate_output_folder=<OUTPUT_FOLDER>/intermediate, PYTHON=python"
}

[ $# -lt 2 ] && usage && exit 1

source_location="${1%/}"
output_folder="${2}"
n_workers="${3:-1}"
sha256sums="${4:-}"

## Settings (same post-processing defaults as recipes/dump.sh)
languages="${languages:-en}"
mul_fallback="${mul_fallback:-en}"
should_keep_intermediate_files="${should_keep_intermediate_files:-no}"
should_disambiguate_types="${should_disambiguate_types:-no}"
should_collapse_languages="${should_collapse_languages:-no}"
default_name_threshold=1
default_format="tsv"
PYTHON="${PYTHON:-python}"
export PYTHONPATH="$(pwd)${PYTHONPATH:+:${PYTHONPATH}}"

content_export="paranames/io/wikidata_content_export.py"
extra_data_folder="${output_folder}/extra_data"

# Default under the output folder, so a rerun with the same arguments resumes
# (parts whose JSONL is up to date are not extracted again).
intermediate_output_folder="${intermediate_output_folder:-${output_folder}/intermediate}"
echo "Intermediate files: ${intermediate_output_folder}"

jsonl_folder="${intermediate_output_folder}/jsonl"
closures_folder="${intermediate_output_folder}/closures"
tsv_folder="${intermediate_output_folder}/tsv"
download_folder="${intermediate_output_folder}/download"
mkdir -p "${output_folder}/combined" "${extra_data_folder}" "${jsonl_folder}" "${closures_folder}" "${tsv_folder}"

case "${source_location}" in
    http://*|https://*) is_url=yes ;;
    *) is_url=no ;;
esac

## List the parts
part_list="${intermediate_output_folder}/parts.txt"
if [ "${is_url}" = "yes" ]
then
    mkdir -p "${download_folder}"
    curl -fsSL --retry 5 "${source_location}/" \
        | grep -o 'href="[^"]*\.xml\.bz2"' \
        | sed -e 's/^href="//' -e 's/"$//' \
        | sort -u > "${part_list}"
    if [ -z "${sha256sums}" ]
    then
        sha256sums="${source_location}/SHA256SUMS"
    fi
else
    find "${source_location}" -maxdepth 1 -name '*.xml.bz2' -exec basename {} \; | sort > "${part_list}"
fi

n_parts=$(wc -l < "${part_list}" | tr -d ' ')
[ "${n_parts}" -eq 0 ] && echo "No *.xml.bz2 parts found in ${source_location}" && exit 1
echo "Parts: ${n_parts}"

case "${sha256sums}" in
    http://*|https://*)
        curl -fsSL --retry 5 -o "${intermediate_output_folder}/SHA256SUMS" "${sha256sums}"
        sha256sums="${intermediate_output_folder}/SHA256SUMS"
        ;;
esac

## Step 1: extract every part (one part per worker)
extract_one () {
    set -euo pipefail
    local name="$1"
    local out="${jsonl_folder}/${name%.xml.bz2}.jsonl.gz"
    local part

    if "${PYTHON}" "${content_export}" is-current -o "${out}" --languages "${languages}"
    then
        echo "[extract] ${name}: up to date, skipping"
        return 0
    fi

    if [ "${is_url}" = "yes" ]
    then
        part="${download_folder}/${name}"
        if ! curl -fsSL --retry 5 --retry-delay 10 -o "${part}.partial" "${source_location}/${name}"
        then
            echo "[extract] ${name}: download failed" >&2
            rm -f "${part}.partial"
            return 1
        fi
        mv "${part}.partial" "${part}"
    else
        part="${source_location}/${name}"
    fi

    local sums_flag=()
    [ -n "${sha256sums}" ] && sums_flag=(--sha256sums "${sha256sums}")

    "${PYTHON}" "${content_export}" extract \
        -i "${part}" -o "${out}" --languages "${languages}" "${sums_flag[@]+"${sums_flag[@]}"}"

    if [ "${is_url}" = "yes" ]
    then
        rm -f "${part}"
    fi
}
export -f extract_one
export jsonl_folder download_folder source_location is_url sha256sums languages PYTHON content_export

echo "[1/6] Extract ${n_parts} parts with ${n_workers} workers..."
# xargs exits non-zero when any job fails; set -e and pipefail stop the run.
if ! tr '\n' '\0' < "${part_list}" | xargs -0 -n 1 -P "${n_workers}" bash -c 'extract_one "$1"' _
then
    echo "[extract] at least one part failed; stopping (rerun to resume)" >&2
    exit 1
fi

missing=0
while IFS= read -r name
do
    if ! "${PYTHON}" "${content_export}" is-current -o "${jsonl_folder}/${name%.xml.bz2}.jsonl.gz" --languages "${languages}"
    then
        echo "[extract] missing or outdated JSONL for ${name}" >&2
        missing=$((missing + 1))
    fi
done < "${part_list}"
if [ "${missing}" -ne 0 ]
then
    echo "[extract] ${missing} of ${n_parts} parts have no up-to-date JSONL; stopping" >&2
    exit 1
fi
n_jsonl=$(find "${jsonl_folder}" -maxdepth 1 -name '*.jsonl.gz' | wc -l | tr -d ' ')
if [ "${n_jsonl}" -ne "${n_parts}" ]
then
    echo "[extract] ${jsonl_folder} has ${n_jsonl} JSONL files for ${n_parts} parts; remove the extra files" >&2
    exit 1
fi

## Step 2: P279 closures from the dump itself
echo "[2/6] Compute LOC/ORG subclass closures..."
"${PYTHON}" "${content_export}" types -i "${jsonl_folder}" -o "${closures_folder}"

## Step 3: type assignment and the intermediate TSV
echo "[3/6] Dump PER/LOC/ORG (languages=${languages}, mul_fallback=${mul_fallback})..."
"${PYTHON}" "${content_export}" dump \
    -i "${jsonl_folder}" -c "${closures_folder}" -o "${tsv_folder}" \
    --languages "${languages}" --mul-fallback "${mul_fallback}" \
    --exclude-languages-file ./data/exclude_these_languages.txt
cp "${tsv_folder}/sitelinks.tsv" "${extra_data_folder}/sitelinks.tsv"
cp "${closures_folder}"/*_closure.txt "${extra_data_folder}/"

## Step 4: postprocess.py (same arguments as recipes/dump.sh)
echo "[4/6] Running postprocess.py"
combined_tsv="${tsv_folder}/combined.tsv"
combined_postprocessed_tsv="${intermediate_output_folder}/combined_postprocessed.tsv"
postprocess_flags=(--should-remove-parentheses)
[ "${should_disambiguate_types}" = "yes" ] && postprocess_flags+=(--should-disambiguate-entity-types)
[ "${should_collapse_languages}" = "yes" ] && postprocess_flags+=(--should-collapse-languages)
"${PYTHON}" paranames/io/postprocess.py \
    -i "${combined_tsv}" -o "${combined_postprocessed_tsv}" -f "${default_format}" \
    -m "${default_name_threshold}" "${postprocess_flags[@]}"

## Step 5: script standardization (same arguments as recipes/dump.sh)
echo "[5/6] Script standardization"
combined_script_standardized_tsv="${output_folder}/paranames.tsv"
"${PYTHON}" paranames/io/script_standardization.py \
    -i "${combined_postprocessed_tsv}" -o "${combined_script_standardized_tsv}" -f tsv \
    --filtered-names-output-file "${extra_data_folder}/filtered_names.tsv" \
    --write-filtered-names --num-workers "${n_workers}"

## Step 6: one subfolder per language
echo "[6/6] Separate into subfolders by language..."
"${PYTHON}" paranames/io/separate_by_language.py \
    --input-file "${combined_script_standardized_tsv}" \
    --lang-column language \
    --io-format "${default_format}" \
    --num-workers "${n_workers}" \
    --use-subfolders \
    --verbose

mv "${combined_script_standardized_tsv}" "${output_folder}/combined/"

if [ "${should_keep_intermediate_files}" = "no" ]
then
    echo "Removing temporary files..."
    rm -rf "${intermediate_output_folder}"
fi

echo "Done: ${output_folder}/combined/paranames.tsv"
