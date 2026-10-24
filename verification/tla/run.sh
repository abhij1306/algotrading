#!/usr/bin/env bash
# Check the HAT TLA+ models with TLC (HAT-VER-030, HAT-VER-032).
#
#   verification/tla/run.sh                  every model against its <Model>.cfg (must pass)
#   verification/tla/run.sh JobScheduler     one model
#
# TLC is tla2tools.jar v1.7.4 (TLC 2.19), pinned by URL and sha256 below, on Java 17 or later
# (JAVA_HOME, else `java` on PATH). The jar is cached in $TLA_CACHE_DIR
# (default ~/.cache/hat-tla); a jar whose sha256 differs is deleted and the run refused.
set -euo pipefail

TLC_VERSION="v1.7.4"
TLC_URL="https://github.com/tlaplus/tlaplus/releases/download/${TLC_VERSION}/tla2tools.jar"
TLC_SHA256="936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CACHE_DIR="${TLA_CACHE_DIR:-${HOME}/.cache/hat-tla}/${TLC_VERSION}"
JAR="${CACHE_DIR}/tla2tools.jar"

sha256_of() {
  local file="$1"
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "${file}" | cut -d' ' -f1
  else shasum -a 256 "${file}" | cut -d' ' -f1; fi
}

fetch_jar() {
  mkdir -p "${CACHE_DIR}"
  if [[ -f "${JAR}" && "$(sha256_of "${JAR}")" == "${TLC_SHA256}" ]]; then return; fi
  rm -f "${JAR}"
  local tmp
  tmp="$(mktemp "${CACHE_DIR}/download.XXXXXX")"
  curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 --retry 3 --output "${tmp}" "${TLC_URL}"
  local got
  got="$(sha256_of "${tmp}")"
  if [[ "${got}" != "${TLC_SHA256}" ]]; then
    rm -f "${tmp}"
    echo "refusing to run: tla2tools.jar ${TLC_VERSION} has sha256 ${got}, expected ${TLC_SHA256}" >&2
    exit 2
  fi
  mv "${tmp}" "${JAR}"
}

find_java() {
  local bin="java"
  if [[ -n "${JAVA_HOME:-}" && -x "${JAVA_HOME}/bin/java" ]]; then bin="${JAVA_HOME}/bin/java"; fi
  if ! command -v "${bin}" >/dev/null 2>&1; then
    echo "Java 17 or later is required (set JAVA_HOME or put java on PATH)" >&2
    exit 2
  fi
  local major
  major="$("${bin}" -version 2>&1 | sed -n '1s/.*version "\([0-9]*\).*/\1/p')"
  if [[ -z "${major}" || "${major}" -lt 17 ]]; then
    echo "Java 17 or later is required, found: $("${bin}" -version 2>&1 | head -1)" >&2
    exit 2
  fi
  echo "${bin}"
}

run_tlc() { # run_tlc <model> <cfg> ; prints TLC output, returns TLC's exit status
  local model="$1" cfg="$2" meta
  meta="$(mktemp -d)"
  local status=0
  (cd "${HERE}" && "${JAVA}" -XX:+UseParallelGC -cp "${JAR}" tlc2.TLC \
      -config "${cfg}" -workers auto -metadir "${meta}" -nowarning "${model}.tla") || status=$?
  rm -rf "${meta}"
  return "${status}"
}

if [[ $# -gt 1 ]]; then echo "usage: run.sh [Model]" >&2; exit 2; fi

fetch_jar
JAVA="$(find_java)"

if [[ $# -eq 1 ]]; then models=("$1"); else
  models=()
  for f in "${HERE}"/*.tla; do models+=("$(basename "${f}" .tla)"); done
fi

failures=0
for model in "${models[@]}"; do
  if [[ ! -f "${HERE}/${model}.tla" || ! -f "${HERE}/${model}.cfg" ]]; then
    echo "no ${model}.tla and ${model}.cfg in ${HERE}" >&2; exit 2
  fi
  echo "== ${model}: must pass"
  if ! run_tlc "${model}" "${model}.cfg" >"${HERE}/.tlc.out" 2>&1; then
    cat "${HERE}/.tlc.out"; echo "FAIL ${model}: TLC reported a problem" >&2; failures=$((failures + 1))
  else
    grep -E "Model checking completed|distinct states found" "${HERE}/.tlc.out" | tail -2
  fi
done
rm -f "${HERE}/.tlc.out"
if [[ ${failures} -gt 0 ]]; then echo "${failures} failure(s)" >&2; exit 1; fi
