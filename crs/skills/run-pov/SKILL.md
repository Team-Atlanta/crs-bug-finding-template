---
name: run-pov
description: Verify a PoV candidate with libCRS run-pov — the canonical way to reproduce a crash inside the correct target environment. A candidate counts as a real PoV only once run-pov reports a non-zero retcode. Also covers rebuilding against a patched build and fetching clean source.
---

# Verify a PoV with libCRS run-pov

`libCRS run-pov` runs the harness on a candidate input inside the correct target
environment. It is the ONLY thing that makes a PoV "verified" — running the harness
binary yourself is fine for exploration, but a candidate counts only when run-pov
reproduces the crash.

```bash
libCRS run-pov <pov_path> <response_dir> --harness {harness}
```
- A non-zero `retcode` (in `<response_dir>/retcode`) means the crash reproduced — a verified PoV.
- Omit `--rebuild-id` to run against the base (original) build.
- `--rebuild-id <id>` runs against a patched/instrumented build (see `apply-patch-build`).

Only write a file to the PoV directory after run-pov confirms a non-zero retcode.

## Related libCRS commands
Rebuild the harness with source edits (e.g. debug logging); returns a rebuild id:
```bash
libCRS apply-patch-build <patch.diff> <response_dir>
```
Fetch clean source for reference (`source_type` = `fuzz-proj` | `target-source`):
```bash
libCRS download-source <source_type> <dst_dir>
```
