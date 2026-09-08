"""Shared fixtures for the QC test suite. Version 1.0.

- SAMPLE_FILES maps firmware -> Path (one representative file per firmware
  generation, chosen from `mea data/`). Tests that need a specific file
  `pytest.skip()` when it's absent.
- raw_bytes / parsed_file are session-cached: each sample file is read
  and parsed once per test session.
- db_conn / test_db_conn open pymysql connections; test_db_conn skips if
  the test DB does not exist so DB tests don't fail on a fresh checkout.
"""
from __future__ import annotations
import hashlib
import sys
from pathlib import Path
from typing import Dict

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

MEA_DATA = REPO_ROOT / "mea data"


# One representative file per firmware generation. Pinned by SHA-256 from
# sample/README.md so a swapped file (same name, different bytes) is caught.
SAMPLE_FILE_SPECS: Dict[str, dict] = {
    "fw216": {
        "path": MEA_DATA / "之前的資料" / "juice2014-10-23" / "141023_121632.mea",
        "sha256": "8dba4f7db7d3b857759384c285bb442b13785a3c944c85c37ef4d10edcb16e1f",
        "shape": (5762, 4500),
        "n_keys": 51,
        "polarity": "positive",
        "boundary": 3013,
    },
    "fw229": {
        "path": MEA_DATA / "之前的資料" / "demo-ketones" / "151030_162302_Blind_Luft_0µgL-1.s.mea",
        "sha256": "bbfe10c617d41d2cc117836809a7c4c381bde6103472727dfd453bbba98419dd",
        "shape": (4285, 4500),
        "n_keys": 56,
        "polarity": "positive",
        "boundary": 3140,
    },
    "fw252": {
        "path": MEA_DATA / "之前的資料" / "mea_1H1-00088" / "250814_152456_F_3.mea",
        "sha256": "0382790d1e602f2db1766aa9f3ab44293beb12df7ba17cadbcd7b98d1ebf7b7a",
        "shape": (8571, 4500),
        "n_keys": 60,
        "polarity": "positive",
        "boundary": 5531,
    },
    "fw473": {
        "path": MEA_DATA / "之前的資料" / "5F1-00554" / "250604_165140.mea",
        "sha256": "2e810daed5868905c2c0a358178a3d0f2bbd488647731ad0d18b65e477248a2c",
        "shape": (12246, 3150),
        "n_keys": 70,
        "polarity": "positive",
        "boundary": 6785,
    },
    "fw482": {
        "path": MEA_DATA / "20260826_茶葉 樣品" / "260826_123948_TEA_1.碧螺春_1.mea",
        "sha256": "2470b71a534aefbda38d5638d1324edb4bc349d6636d1471db36f2a0ad46a4c1",
        "shape": (14289, 3150),
        "n_keys": 68,
        "polarity": "positive",
        "boundary": 5993,
    },
}


def _skip_if_missing(spec: dict) -> Path:
    p: Path = spec["path"]
    if not p.exists():
        pytest.skip(f"sample file absent: {p}")
    return p


@pytest.fixture(scope="session")
def sample_specs() -> Dict[str, dict]:
    return SAMPLE_FILE_SPECS


@pytest.fixture(scope="session", params=list(SAMPLE_FILE_SPECS.keys()))
def sample_firmware(request) -> str:
    """Parametrized fixture: yields fw216, fw229, fw252, fw473, fw482 in turn."""
    return request.param


@pytest.fixture(scope="session")
def sample_path(sample_firmware) -> Path:
    return _skip_if_missing(SAMPLE_FILE_SPECS[sample_firmware])


# session-scoped byte cache — 90 MB files are heavy to re-read across many tests
_bytes_cache: Dict[str, bytes] = {}


@pytest.fixture(scope="session")
def sample_bytes(sample_firmware, sample_path) -> bytes:
    if sample_firmware not in _bytes_cache:
        _bytes_cache[sample_firmware] = sample_path.read_bytes()
    return _bytes_cache[sample_firmware]


@pytest.fixture(scope="session")
def sample_spec(sample_firmware) -> dict:
    return SAMPLE_FILE_SPECS[sample_firmware]


_parsed_cache = {}


@pytest.fixture(scope="session")
def parsed_sample(sample_firmware, sample_bytes):
    """Returns (header, matrix, meta) — parsed once per session per firmware."""
    from mea_parser import split_mea
    if sample_firmware not in _parsed_cache:
        _parsed_cache[sample_firmware] = split_mea(sample_bytes)
    return _parsed_cache[sample_firmware]


# --- DB fixtures (opt-in) ---

DB_CFG = dict(host="127.0.0.1", port=3306, user="shengic", password="sirirat",
              charset="utf8mb4", connect_timeout=5)


@pytest.fixture(scope="session")
def db_conn():
    """Read-mostly connection to the production DB. Tests using this MUST
    NOT write. For destructive tests use test_db_conn."""
    import pymysql
    try:
        conn = pymysql.connect(database="gc-ims_database", **DB_CFG)
    except pymysql.err.OperationalError as e:
        pytest.skip(f"MySQL unavailable: {e}")
    yield conn
    conn.close()


@pytest.fixture(scope="function")
def test_db_conn():
    """Destructive tests. Skips if `gc-ims_database_test` does not exist —
    see tests/README.md for setup."""
    import pymysql
    try:
        conn = pymysql.connect(database="gc-ims_database_test", **DB_CFG,
                               autocommit=False)
    except pymysql.err.OperationalError as e:
        pytest.skip(f"test DB not available: {e}")
    yield conn
    conn.rollback()
    conn.close()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()
