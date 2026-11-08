"""importer/report.py — the plain-text run report shared by the snapshot and file imports."""

from __future__ import annotations

from dataclasses import dataclass, field

BUCKETS = ("source_count", "new", "duplicate", "linked", "malformed", "unmatched", "errors")


@dataclass
class Report:
    title: str
    dry_run: bool
    status: str = "done"  # done | dry_run | blocked | failed
    tables: dict[str, dict[str, int]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # per-row malformed/unmatched/error reasons

    def render(self, max_problems: int = 20) -> str:
        head = (
            f"{self.title} — {'DRY RUN (nothing written)' if self.dry_run else self.status.upper()}"
        )
        lines = [head]
        for name, c in self.tables.items():
            lines.append(f"  {name}: " + ", ".join(f"{k}={c.get(k, 0)}" for k in BUCKETS))
        lines += [f"  {n}" for n in self.notes]
        if self.problems:
            lines.append(f"  problems ({len(self.problems)}):")
            lines += [f"    {p}" for p in self.problems[:max_problems]]
            if len(self.problems) > max_problems:
                lines.append(f"    … {len(self.problems) - max_problems} more")
        return "\n".join(lines)
