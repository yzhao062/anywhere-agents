# State Recovery Reference (`gather`, `report-state`, `snapshot-tail`)

Read this when a run was interrupted, or when `gather` replaces `monitor`.

## gather usage

`gather` prints `DONE` for any non-empty quiet file, a `FALLBACK` included; `DONE` is not a usable result.

```
scripts/gather.sh <result-file-1> <result-file-2> ...
```

- Prints `GATHER-START count=N timeout=Ss`, then `DONE <abs-path>` per file as it lands; exits 0 when
  all land, exits 2 with `TIMEOUT remaining=<k>`.
- A file is "landed" when it exists, is non-empty, and has been quiet for the stable window
  (default 10s); no startup-snapshot race.
- **Use a fresh result path per unit per run** (delete any stale file before dispatch). Have each unit
  write its result in one operation.

## report-state usage

```
scripts/report-state.sh   [--root DIR] [--json] [--summary] [--sort path|tail-bytes-desc]
                          [--min-tail-bytes N] [--include-legacy-pid]
```

Read-only. It inspects `prun-task-*` directories left behind by earlier runs and writes nothing at
all, which `tests/test_prun_report.py` checks by hashing the tree before and after a run. Reach for
it when a fan-out was interrupted and you need to know which unit output survived. `--root` repeats,
and defaults to the system temp directory.

Every unit carries two independent fields instead of one verdict. A single label such as
"salvageable" would read as permission to act, and this command cannot support that reading without
the process identity it deliberately does not record.

| `result_path_state` | Meaning |
|---|---|
| `resolved` | the unit recorded a result path and it could be read |
| `absent-entry` | no `result-file` entry was written |
| `invalid-entry` | the entry was empty, or a relative path escaping its unit |
| `unreadable` | the entry exists but could not be read |

| `result` | Meaning |
|---|---|
| `present` | the result file exists and holds bytes |
| `empty` | the result file exists and is zero bytes |
| `missing` | the recorded path does not exist |
| `unknown` | nothing is claimed: either the path never resolved, or it resolved and the target could not be observed |

`result` is `unknown` for every `result_path_state` other than `resolved`, and `resolved` may also
carry it. Only `FileNotFoundError` proves a target is gone; a denial or an I/O error yields
`resolved`/`unknown` plus an entry in that unit's `errors`, so a failed observation is never
reported as an outcome. No other pairing can be emitted, and
`test_no_illegal_pair_can_be_emitted` checks that against the table the module exports.

Remaining JSON fields:

| Field | Meaning |
|---|---|
| `schema_version` | `1`; bump on any field change |
| `roots` | absolute directories inspected |
| `unit_count` | units inspected, counted before any display filter |
| `discovery_errors` | roots or matching entries that could not be listed or stated |
| `unit` | absolute path of the unit directory |
| `tail_bytes` | size of the unit's `tail`, `0` when absent, or `null` when it could not be stated or is not a regular file |
| `result_target` | the resolved result path, or `null` |
| `errors` | per-unit observation failures; see the table below |
| `legacy_pid_unverified` | shown only under `--include-legacy-pid` |
| `safety` | the sentence below, present on every run |

Each `errors` entry is `{"stage": <where>, "error": <value>}`. The value is an exception class name,
or one of two names for a condition that raises nothing: `NotARegularFile` when the path exists but
is a directory, FIFO, or device, and `EntryTooLarge` when a `result-file` or `dispatch-pid` entry
exceeds 64 KiB. That size limit reports rather than truncates. A truncated entry can strip down to
a real path and be mistaken for a complete one. Consumers branch on `stage`:

| `stage` | What could not be observed |
|---|---|
| `result-entry` | the unit's `result-file` exists but could not be read |
| `result-target` | the recorded path could not be stated, or is not a regular file |
| `result` | classification raised unexpectedly; the unit is still reported |
| `tail` | the unit's `tail` could not be stated, or is not a regular file |
| `legacy-pid` | `dispatch-pid` exists but could not be read, under `--include-legacy-pid` |

Discovery failures sit apart from any unit, in a top-level `discovery_errors` array whose entries
carry `stage` (`root` or `unit-entry`), the offending `root` or `unit`, and `error`. They are
separate because a root that cannot be listed produces no unit to attach a failure to, and used to
read as an empty corpus. Any entry in either place sets exit `1`.

`--summary` adds two byte counters that never overlap. `missing_or_empty_result` covers units whose
result path resolved to a file that is missing or empty. `unresolved` covers units whose result was
never classified while their tail still holds bytes. Each counter names what was observed rather
than what may be done about it, because neither a missing target nor an empty one proves that no
other copy exists or that a live producer will not fill it.


Under `--json`, those counters arrive in a `summary` object:

| Summary field | Meaning |
|---|---|
| `units` | units inspected, matching `unit_count` |
| `by_result` | count per `result` value |
| `by_path_state` | count per `result_path_state` value |
| `missing_or_empty_result_units` / `missing_or_empty_result_bytes` | resolved path, result file missing or empty, tail holds bytes |
| `unresolved_units` / `unresolved_bytes` | result never classified, tail holds bytes |
`--min-tail-bytes` hides small units from the listing and moves no unit between classes; `unit_count`
still counts them. `--include-legacy-pid` stays off by default. A recorded PID may be stale, or
reused by an unrelated process, so it can never show that a worker is alive.

Exit codes: `0` every root was listed and every unit inspected cleanly, `1` at least one entry
was recorded in a unit's `errors` or in `discovery_errors` while everything readable was still
reported, `2` a usage error. An unreadable root is never reported as an empty one.

## snapshot-tail usage

```
scripts/snapshot-tail.sh   --unit DIR [--dest DIR | --output FILE] [--json]
```

Copies one unit's `tail` into a ZIP holding exactly two members, `tail.bin` and `manifest.json`,
both stored without compression. Only a regular file, or a symlink to one, may be snapshotted; a
directory, FIFO, or device exits `4` and publishes nothing. Without that rule a device such as
`/dev/null` reported zero bytes and published an empty archive as a complete capture, and a FIFO
with no writer blocked the open indefinitely. The copy is byte-for-byte, so a tail carrying NUL or CR arrives
unchanged. Given neither `--dest` nor `--output`, the archive lands in a per-user state directory:
`$XDG_STATE_HOME/anywhere-agents/prun/snapshots`, falling back to `~/.local/state` when
that variable is unset.

On POSIX the command creates the directory mode `0700` and the archive mode `0600`. A snapshot
extends the lifetime of prompts and tool output, so a directory that already exists and is group- or
world-accessible is refused, with the `chmod` that fixes it named in the message.

Publication goes through `os.link`. That is the one portable operation which is both atomic and
refuses to replace: `os.replace` overwrites, `os.rename` differs by platform, and checking first
races. An existing destination therefore exits `3` and leaves the file byte-identical. Six
concurrent attempts on one name produce exactly one winner. Any other link failure exits `6` rather
than falling back to an operation that could overwrite.

| Manifest field | Meaning |
|---|---|
| `schema_version` | `1` |
| `captured_at` | UTC timestamp of the capture |
| `source_path` | absolute path of the tail that was read |
| `source_size_at_open` | size taken from `fstat` on the already-open handle |
| `bytes_copied` | bytes actually written |
| `sha256` | digest of the copied bytes, re-verified after the archive closes |
| `source_may_be_live` | always `true` |
| `capture_outcome` | `complete_bounded_read` when the two counts agree, `short_read` otherwise |
| `note` | records that equal counts do not prove the source held still |

The read is bounded by `source_size_at_open` and best-effort: equal counts do not show that the
source held still, because bytes from different generations of a growing file, or from a
truncate-and-regrow, can total the same number. Read `complete_bounded_read` as "the reader returned
`source_size_at_open` bytes before EOF", never as a consistent point-in-time copy.

JSON output adds `published`, the final path, and `warning`, which is `null` on a clean run. A
warning appears when the archive is linked into place but the temporary file could not be removed.
The snapshot is valid in that case, so the command still exits `0`.

Exit codes: `0` published, `3` the destination already existed, `4` the tail could not be opened
or is not a regular file,
`5` archive validation failed, `6` publication failed. Every failure other than `3` leaves no file
at the final name.

### The safety sentence

**Snapshotting a tail is the only safe operation offered here. This output does not establish that
deleting, overwriting, or promoting any unit is safe.**

`report-state` prints those words on every run, in both text and JSON. `snapshot-tail` does not
repeat them, so apply them yourself after a successful capture: holding a snapshot does not make the
unit disposable. Deciding that a unit is finished needs process identity, which this slice records
nowhere. See anywhere-agents#29 Part B.
