# Sample .mea inventory

The four cross-validation files are NOT committed (size). Originals live
on the lab NAS; identity is pinned by SHA-256 (verify after copying).

| file | bytes | sha256 | firmware | matrix | polarity |
|---|---|---|---|---|---|
| 141023_121632.mea | 51,861,013 | 8dba4f7db7d3b857759384c285bb442b13785a3c944c85c37ef4d10edcb16e1f | 2.16 (2014) | 5762x4500 | positive |
| 260210_095729_FISH_MEAT_BLANK.mea | 77,144,541 | 5af6eb45c35d22734de456a7ffa53c7d3f10ea0e2c61bfd4a36609c776fb6edb | 2.52 | 8571x4500 | negative |
| 250814_152456_F_3.mea | 77,144,531 | 0382790d1e602f2db1766aa9f3ab44293beb12df7ba17cadbcd7b98d1ebf7b7a | 2.52 | 8571x4500 | positive |
| 260826_123948_TEA_1_碧螺春_1.mea | 90,026,693 | 2470b71a534aefbda38d5638d1324edb4bc349d6636d1471db36f2a0ad46a4c1 | 4.82 | 14289x3150 | positive |

Unit tests (mea_parser) pin split boundaries and key counts to these files:
5541/60 keys (FISH), 5993/68 (TEA), and byte-exact int16 reshape on all four.
