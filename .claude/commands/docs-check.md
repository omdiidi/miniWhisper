---
description: Diff the file→doc map in docs/OVERVIEW.md against git mtimes; flag any docs that are older than the code they document.
---

# /docs-check

Audit documentation freshness without modifying anything.

## What it does

1. Read `docs/OVERVIEW.md` and parse the markdown table that maps source files to documentation files.
2. For each `(code_path, doc_path)` row:
   - `code_ts = git log -1 --format=%ct -- <code_path>`
   - `doc_ts  = git log -1 --format=%ct -- <doc_path>`
   - If `code_ts > doc_ts`, flag as **stale** and record the gap (`code_ts - doc_ts` in days).
3. Also flag:
   - Code files in `server/src/`, `client/WisprAlt/`, or `scripts/` that are NOT mentioned in `OVERVIEW.md` (orphaned code without a doc mapping).
   - Doc files in `docs/` that are NOT referenced (orphaned docs).

## Output

Print a markdown table:

| Code file | Doc file | Code mtime | Doc mtime | Gap (days) |
|---|---|---|---|---|
| ... | ... | ... | ... | ... |

Followed by:
- **Orphaned code files** (no doc mapping)
- **Orphaned doc files** (not referenced)
- **Summary**: N stale docs, M orphaned code, K orphaned docs.

## Do NOT

- Do not auto-edit any docs. This command is **report-only**.
- Do not skip the orphan checks; they catch the most common drift mode (a new file added with no doc updated).
- Do not push to git.

## Suggest next steps

If issues are found, suggest the user:
- Run the `/document` skill for stale entries.
- Manually update `docs/OVERVIEW.md` if a new code file is missing from the map.
