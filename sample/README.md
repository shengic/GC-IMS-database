<!-- Version 1.1 -->
# Sample .mea inventory

The cross-validation files are NOT committed (size). Originals live on
the lab NAS; identity is pinned by SHA-256 (verify after copying).

Machine column: `®` variants written as `FlavourSpec®` or `FlavourSpecR`
(latin-1 encoding drift) are the same product; `GC-IMS` is a separate
product line (pump-controlled instead of dual-EPC).

| # | file | bytes | sha256 | firmware | machine (serial) | matrix | keys | polarity |
|---|---|---|---|---|---|---|---|---|
| 1 | 141023_121632.mea | 51,861,013 | 8dba4f7db7d3b857759384c285bb442b13785a3c944c85c37ef4d10edcb16e1f | 2.16 (2014) | FlavourSpec® (1H1-00044) | 5762x4500 | 51 | positive |
| 2 | 260210_095729_FISH_MEAT_BLANK.mea | 77,144,541 | 5af6eb45c35d22734de456a7ffa53c7d3f10ea0e2c61bfd4a36609c776fb6edb | 2.52 | FlavourSpec® (1H1-00088) | 8571x4500 | 60 | negative |
| 3 | 250814_152456_F_3.mea | 77,144,531 | 0382790d1e602f2db1766aa9f3ab44293beb12df7ba17cadbcd7b98d1ebf7b7a | 2.52 | FlavourSpec® (1H1-00088) | 8571x4500 | 60 | positive |
| 4 | 260826_123948_TEA_1_碧螺春_1.mea | 90,026,693 | 2470b71a534aefbda38d5638d1324edb4bc349d6636d1471db36f2a0ad46a4c1 | 4.82 | FlavourSpec® (5H4-00615) | 14289x3150 | 68 | positive |
| 5 | 151030_162302_Blind_Luft_0µgL-1.s.mea | 38,568,140 | bbfe10c617d41d2cc117836809a7c4c381bde6103472727dfd453bbba98419dd | 2.29 (2015) | FlavourSpecR (1H1-00081) | 4285x4500 | 56 | positive |
| 6 | 250604_165140.mea | 77,156,585 | 2e810daed5868905c2c0a358178a3d0f2bbd488647731ad0d18b65e477248a2c | 4.73 | GC-IMS (5F1-00554) | 12246x3150 | 70 | positive |
| 7 | 250605_104340.mea | 102,880,441 | e1e2f08addb60513ba72f9f431ccbdbc366ba7a61bd81bb388d3b32893b460e4 | 4.73 | GC-IMS (5F1-00554) | 16329x3150 | 70 | positive |
| 8 | 240328_122104_KETONE_MIX_60T.mea | 151,718,742 | b6574751bc689cb20656791246f176f204d42cebe6662b091514ce27ae4a767c | 2.52 | FlavourSpec® (1H1-00123) | 16857x4500 | 60 | positive |

Type coverage:
- 6 firmware generations (2.16, 2.29, 2.52, 4.73, 4.82) across 5 instrument serials.
- 2 product lines: FlavourSpec (dual-EPC controller) and GC-IMS (pump-controlled — has `Pump 1 flow/pressure` telemetry series absent from FlavourSpec).
- `.s.mea` extension variant (fw 2.29 era, type 5).
- Longest GC program observed: 16,857 spectra (type 8, TEA2 60-min ramp).

Unit tests (mea_parser) pin split boundaries, key counts, and byte-exact
int16 reshape on every row above. `split_mea()` reference implementation
passed with `delta=0` on all 8 types.
