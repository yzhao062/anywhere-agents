# Composed-consumer fixture

Fixed copies of the three passive pack bodies the maintainer's consumers compose into `AGENTS.md`, used by `tests/test_composed_consumer_size.py` to check a composed consumer against `CONSUMER_CEILING_BYTES` without a network fetch. Refresh a copy when its pack body changes on purpose, and record the new size and hash here.

| Pack | Source | Path | Bytes | SHA-256 |
|---|---|---|---|---|
| `agent-style` | github.com/yzhao062/agent-style at `v0.9.0` | `docs/rule-pack-compact.md` | 22,544 | `169f0576e4aaf51faae3207ae7ff5ab91c0f4afb862487b120ee9f6e17d3077c` |
| `profile` | github.com/yzhao062/agent-pack (compact bodies, 2026-11-22) | `docs/rule-pack-compact.md` | 4,405 | `2a41459cd8e4bb4c91540cfce1ac7ea6d2da24a6a5b4ca1c6472bf2214f6e2a5` |
| `paper-workflow` | github.com/yzhao062/agent-pack (compact bodies, 2026-11-22) | `docs/paper-workflow-compact.md` | 7,569 | `ba0aa427072ace52366c289a2782641f79ad9ed62750daa02c9a80630dca58b5` |

The composition order is the order consumers use: the bundled `agent-style` first, then the two `agent-pack` packs from `agent-config.yaml`.
