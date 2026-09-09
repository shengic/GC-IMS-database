"""`find_rip` correctness. Version 1.1.

VOCal-compatible: argmax over RT=0 row after skipping the first 200 drift
samples. Tests pin the algorithm's contract from DESIGN §5b.
"""
import numpy as np
import pytest

from mea_parser import find_rip, RIP_ROW0_SKIP


class TestFindRipOnRealFiles:
    def test_returns_valid_index(self, parsed_sample):
        _, matrix, _ = parsed_sample
        rip_idx, signed, polarity, weak = find_rip(matrix)
        assert RIP_ROW0_SKIP <= rip_idx < matrix.shape[1]

    def test_polarity_matches_recorded(self, parsed_sample, sample_spec):
        _, matrix, _ = parsed_sample
        _, _, polarity, _ = find_rip(matrix)
        assert polarity == sample_spec["polarity"]

    def test_signed_value_from_row0_not_column_mean(self, parsed_sample):
        """VOCal method uses row0, not column mean — pin the source."""
        _, matrix, _ = parsed_sample
        rip_idx, signed, _, _ = find_rip(matrix)
        assert float(matrix[0, rip_idx]) == pytest.approx(signed)

    def test_not_weak_on_normal_files(self, parsed_sample):
        """The 5 pinned representative files all have well-formed RIPs;
        weak-RIP flag should be False for them."""
        _, matrix, _ = parsed_sample
        _, _, _, weak = find_rip(matrix)
        assert weak is False


class TestFindRipSkipsPathologicalLeading:
    """Regression: `251020_130723_2WK_4_2.mea` had an unconstrained-argmax
    winner at drift index 18 from a leading transient. The 200-sample skip
    rejects this class of failure."""

    def test_leading_spike_before_200_is_ignored(self):
        # 3000-drift matrix, 100 spectra. Massive spike at drift index 18
        # (would fool unconstrained argmax), true RIP planted at 1200.
        m = np.zeros((100, 3000), dtype=np.int16)
        m[0, 18] = 30000     # leading transient — would win argmax(row0)
        m[0, 1200] = 5000    # true RIP (plausible location)
        rip_idx, _, _, _ = find_rip(m)
        assert rip_idx == 1200, f"expected 1200 (skipped the drift-18 spike), got {rip_idx}"

    def test_start_parameter_is_configurable(self):
        m = np.zeros((10, 2000), dtype=np.int16)
        m[0, 150] = 5000
        rip_idx, _, _, _ = find_rip(m, start=100)
        assert rip_idx == 150

    def test_matrix_narrower_than_start_raises(self):
        m = np.zeros((10, 100), dtype=np.int16)
        with pytest.raises(ValueError):
            find_rip(m, start=200)


class TestFindRipPolarity:
    def test_positive_rip_signals_positive_polarity(self):
        m = np.zeros((10, 3000), dtype=np.int16)
        m[0, 1000] = 5000
        _, signed, polarity, _ = find_rip(m)
        assert signed > 0
        assert polarity == "positive"

    def test_negative_rip_signals_negative_polarity(self):
        m = np.zeros((10, 3000), dtype=np.int16)
        m[0, 1000] = -5000
        _, signed, polarity, _ = find_rip(m)
        assert signed < 0
        assert polarity == "negative"


class TestFindRipWeakFlag:
    def test_flat_noise_flags_weak(self):
        """A row where max ≈ median magnitude → weak flag."""
        rng = np.random.default_rng(0)
        m = rng.integers(-100, 100, size=(10, 3000), dtype=np.int16)
        _, _, _, weak = find_rip(m)
        assert weak is True

    def test_clear_rip_not_weak(self):
        rng = np.random.default_rng(0)
        m = rng.integers(-100, 100, size=(10, 3000), dtype=np.int16)
        m[0, 1500] = 20000   # winner >> 3x median magnitude
        _, _, _, weak = find_rip(m)
        assert weak is False
