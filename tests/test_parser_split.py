"""`split_mea` correctness across all firmware generations. Version 1.1.

Pins the byte-exact invariant from DESIGN §2b: arithmetic back-calc split
must match heuristic first-non-printable-byte within a few bytes; header
key count, matrix shape, and boundary must all match the recorded values
in `sample/README.md` and `docs/DESIGN.md`.
"""
import pytest
import numpy as np

from mea_parser import split_mea, MeaParseError
from conftest import sha256_bytes


class TestSplitMeaOnRealFiles:
    def test_sha256_matches_pinned(self, sample_bytes, sample_spec):
        """Content integrity: bytes on disk match README's SHA-256 anchor."""
        assert sha256_bytes(sample_bytes) == sample_spec["sha256"]

    def test_boundary_matches_recorded(self, sample_bytes, sample_spec):
        _, _, meta = split_mea(sample_bytes)
        assert meta["header_bytes"] == sample_spec["boundary"]

    def test_delta_within_tolerance(self, sample_bytes):
        """Arith vs. heuristic split methods must agree within 4 bytes."""
        _, _, meta = split_mea(sample_bytes)
        assert abs(meta["boundary_delta"]) <= 4

    def test_matrix_shape_byte_exact(self, sample_bytes, sample_spec):
        _, matrix, _ = split_mea(sample_bytes)
        assert matrix.shape == sample_spec["shape"]
        assert matrix.dtype == np.dtype("<i2")

    def test_header_key_count(self, sample_bytes, sample_spec):
        header, _, _ = split_mea(sample_bytes)
        assert len(header) == sample_spec["n_keys"]

    def test_matrix_byte_count_matches_geometry(self, sample_bytes):
        header, matrix, meta = split_mea(sample_bytes)
        expected_matrix_bytes = matrix.shape[0] * matrix.shape[1] * 2
        actual_matrix_bytes = len(sample_bytes) - meta["header_bytes"]
        assert expected_matrix_bytes == actual_matrix_bytes


class TestSplitMeaErrors:
    def test_truncated_file_raises(self, sample_bytes):
        truncated = sample_bytes[: len(sample_bytes) // 2]
        with pytest.raises(MeaParseError):
            split_mea(truncated)

    def test_missing_geometry_keys_raises(self):
        # A header without Chunks count / Chunk sample count — arith
        # back-calc has nothing to work with.
        fake = b"Some header without geometry keys\n= dummy\n" + b"\x00" * 200
        with pytest.raises(MeaParseError, match="geometry keys not found"):
            split_mea(fake)

    def test_declared_larger_than_file_raises(self):
        # geometry keys present but implying more matrix bytes than exist
        fake_header = (
            "Chunks count = 100000\n"
            "Chunk sample count = 100000\n"
        ).encode("latin-1")
        # short body
        fake = fake_header + b"\x00" * 100
        with pytest.raises(MeaParseError, match="larger than file"):
            split_mea(fake)
