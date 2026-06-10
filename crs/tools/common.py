"""Shared helpers for the runner tools."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger("crs.tools")

# Where each build variant is downloaded to when not supplied explicitly, and
# the libCRS build-output path it is fetched from (must match crs.yaml outputs).
# Each variant maps to (build-output KEY, local download path). The key is the
# libCRS submit/download identifier (must match the compile_* scripts and the
# `outputs` in oss-crs/crs.yaml); keep keys flat (single token), not paths.
BUILD_VARIANTS = {
    "asan": ("build", Path("/out")),
    "debug": ("debug", Path("/work/debug-build")),
    "coverage": ("coverage", Path("/work/coverage-build")),
    "afl": ("afl", Path("/work/afl-build")),
}

# CodeQL database (a directory), submitted by the codeql-build phase under the
# `codeql` key and downloaded here for the crs-codeql tool.
CODEQL_DB_REMOTE = "codeql"
CODEQL_DB_LOCAL = Path("/work/codeql/db")


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


# --- CPU allocation ---------------------------------------------------------
# The container is pinned to a core set via the compose `cpuset` (exposed as
# OSS_CRS_CPUSET, e.g. "2-7"). We split it in half so the two fuzzing engines
# don't contend: crs-fuzz (libFuzzer) gets the FIRST half, crs-afl (AFL++) the
# SECOND half.
def _parse_cpuset(s: str) -> list[int]:
    cpus: list[int] = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            cpus.extend(range(int(lo), int(hi) + 1))
        else:
            cpus.append(int(part))
    return cpus


def available_cpus() -> list[int]:
    """CPUs this container may use: OSS_CRS_CPUSET if set, else process affinity."""
    s = os.environ.get("OSS_CRS_CPUSET", "").strip()
    if s:
        cpus = _parse_cpuset(s)
        if cpus:
            return sorted(set(cpus))
    try:
        return sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return list(range(os.cpu_count() or 1))


def split_cpus() -> tuple[list[int], list[int]]:
    """Partition the cores: (first half -> crs-fuzz, second half -> crs-afl).

    An odd extra core goes to crs-fuzz. With a single core, both halves share it.
    """
    cpus = available_cpus()
    if len(cpus) < 2:
        only = cpus or [0]
        return only, only
    half = (len(cpus) + 1) // 2
    return cpus[:half], cpus[half:]


def set_affinity(cpus) -> None:
    """Best-effort pin the current process to `cpus` (use as a preexec_fn)."""
    try:
        os.sched_setaffinity(0, set(cpus))
    except (AttributeError, OSError, ValueError):
        pass


def run(cmd, *, timeout=None, env=None, cwd=None, capture=True) -> subprocess.CompletedProcess:
    """Run a command, returning the CompletedProcess (never raises on non-zero)."""
    argv = [str(c) for c in cmd]
    logger.info("$ %s", " ".join(argv))
    return subprocess.run(
        argv,
        timeout=timeout,
        env={**os.environ, **(env or {})},
        cwd=str(cwd) if cwd else None,
        capture_output=capture,
        text=True,
    )


def libcrs_download(remote: str, dst: Path) -> bool:
    """Fetch a build output via libCRS. Returns False if unavailable (e.g. local)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not have("libCRS"):
        return False
    try:
        r = run(["libCRS", "download-build-output", remote, str(dst)], timeout=900)
        if r.returncode != 0:
            logger.warning("libCRS download-build-output %s -> rc=%s: %s", remote, r.returncode, r.stderr.strip())
            return False
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("libCRS download-build-output %s failed: %s", remote, e)
        return False


def libcrs_submit(data_type: str, path: Path) -> bool:
    """Submit a single file to oss-crs via `libCRS submit` (content-deduped)."""
    if not have("libCRS"):
        return False
    try:
        r = run(["libCRS", "submit", data_type, str(path)], timeout=120)
        if r.returncode != 0:
            logger.warning("libCRS submit %s %s -> rc=%s: %s",
                           data_type, path, r.returncode, (r.stderr or "").strip())
            return False
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("libCRS submit %s %s failed: %s", data_type, path, e)
        return False


def submit_files(data_type: str, directory: Path) -> int:
    """Submit every regular file directly under `directory`. Returns the count."""
    if not directory.exists():
        return 0
    n = 0
    for f in sorted(directory.iterdir()):
        if f.is_file() and not f.name.startswith("."):
            if libcrs_submit(data_type, f):
                n += 1
    return n


def _non_empty_dir(p: Path) -> bool:
    return p.is_dir() and any(p.iterdir())


# --- Corpus management ------------------------------------------------------
# One canonical, accumulating corpus per harness, so crs-fuzz and crs-coverage
# share state instead of each call guessing a directory:
#   * seeded from the boot seeds (SEED_SRC) on first use,
#   * grown by crs-fuzz (libFuzzer writes back only coverage-increasing inputs),
#   * read by crs-coverage as its default corpus,
#   * minimized on demand via `crs-fuzz --merge` (libFuzzer -merge=1).
# Crash reproducers are kept OUT of this pool — they go to the PoV dir.
CORPUS_ROOT = Path(os.environ.get("CRS_CORPUS_DIR", "/artifacts/corpus"))
SEED_SRC = Path(os.environ.get("CRS_SEED_DIR", "/work/seeds"))


def corpus_dir(harness: str) -> Path:
    """The canonical accumulating corpus directory for `harness` (created)."""
    d = CORPUS_ROOT / harness
    d.mkdir(parents=True, exist_ok=True)
    return d


def seed_from_boot(dst: Path) -> int:
    """Copy the boot seeds (SEED_SRC) into `dst`, content-deduped.

    Files are named by their SHA-1 — matching libFuzzer's own corpus naming — so
    this is idempotent (identical content maps to the same name) and never
    creates duplicates. Returns the number of files added.
    """
    if not SEED_SRC.exists():
        return 0
    dst.mkdir(parents=True, exist_ok=True)
    existing = {p.name for p in dst.iterdir() if p.is_file()}
    added = 0
    for f in sorted(SEED_SRC.rglob("*")):
        if not f.is_file() or f.name.startswith("."):
            continue
        try:
            name = hashlib.sha1(f.read_bytes()).hexdigest()
        except OSError:
            continue
        if name in existing:
            continue
        shutil.copyfile(f, dst / name)
        existing.add(name)
        added += 1
    if added:
        logger.info("seeded %d boot seed(s) from %s into %s", added, SEED_SRC, dst)
    return added


def resolve_build_dir(variant: str, explicit: str | os.PathLike | None) -> Path:
    """Return the directory holding a build variant's harness binaries.

    Order: explicit --build-dir, then a pre-existing local default, then a
    libCRS download. Falls back to the asan build if a variant is missing.
    """
    if explicit:
        return Path(explicit)

    remote, local = BUILD_VARIANTS[variant]
    if _non_empty_dir(local):
        return local
    if libcrs_download(remote, local) and _non_empty_dir(local):
        return local

    if variant != "asan":
        logger.warning("%s build unavailable; falling back to asan build", variant)
        return resolve_build_dir("asan", None)
    return local


def harness_path(build_dir: Path, harness: str) -> Path:
    return Path(build_dir) / harness


def require_harness(build_dir: Path, harness: str) -> Path:
    hb = harness_path(build_dir, harness)
    if not hb.exists():
        raise SystemExit(f"harness not found: {hb}")
    return hb


def symbolizer_env() -> dict:
    """ASAN/LLVM symbolizer env so crashes show symbolized frames."""
    env = {}
    sym = shutil.which("llvm-symbolizer")
    if sym:
        env["ASAN_SYMBOLIZER_PATH"] = sym
    return env
