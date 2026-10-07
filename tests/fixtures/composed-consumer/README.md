# Composed-consumer fixture

Fixed copies of the three passive pack bodies the maintainer's consumers compose into `AGENTS.md`, used by `tests/test_composed_consumer_size.py` to hold a composed consumer under 61,440 bytes without a network fetch. Refresh a copy when its pack body changes on purpose, and record the new size and hash here.

| Pack | Source | Path | Bytes | SHA-256 |
|---|---|---|---|---|
| `agent-style` | github.com/yzhao062/agent-style at `v0.5.0` | `docs/rule-pack-compact.md` | 22,374 | `5d7fda1d5238e2c769d494fe572f69318e7ea75940e2c6d6a6284dc04b7eaf2c` |
| `profile` | github.com/yzhao062/agent-pack (compact bodies, 2026-10-06) | `docs/rule-pack-compact.md` | 4,783 | `ffde57bd3c254e4e1508c3a257b1f9db04157b7e3aad56cadb8f18500e430d8e` |
| `paper-workflow` | github.com/yzhao062/agent-pack (compact bodies, 2026-09-17) | `docs/paper-workflow-compact.md` | 5,630 | `f4fcef7341bc90e6ba88c947c4961c5548a8cd7481b5881de449837d5160d6f3` |

The composition order is the order consumers use: the bundled `agent-style` first, then the two `agent-pack` packs from `agent-config.yaml`.
