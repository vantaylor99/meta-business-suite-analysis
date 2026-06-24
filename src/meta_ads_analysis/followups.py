"""Due-date-aware follow-up tasks for ad accounts (context-efficient by design).

These are operator/agent reminders about ongoing account work — e.g. "evaluate the new ad after
a week and prune to the best copy". They are deliberately SEPARATE from agent-created Jira/`calls`
tickets: those track software work; these track account-management follow-ups.

The whole point is to not burn context: an agent begins an account check-in by running
`followups due --account <slug>`, which prints ONLY tasks that are due/overdue (title + due date +
id) — never the bodies of tasks that aren't actionable yet. Read a task's body only when it's due.

Layout (committed to git, so every agent/machine sees it):
    followups/<account_slug>/<due>-<slug>.md      # open tasks
    followups/<account_slug>/done/<...>.md         # completed (archived)

Each file is markdown with simple frontmatter: title, account, due (YYYY-MM-DD), status, created.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .config import PROJECT_ROOT
from .utils import slugify_name

FOLLOWUPS_ROOT = PROJECT_ROOT / "followups"


@dataclass(slots=True)
class Followup:
    path: Path
    title: str
    account: str
    due: date | None
    status: str
    created: str | None
    body: str

    @property
    def task_id(self) -> str:
        return self.path.stem


def _parse(path: Path) -> Followup:
    text = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    body = text
    if text.lstrip().startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            _, fm, body = parts
            for line in fm.strip().splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    meta[key.strip()] = value.strip()
    due: date | None = None
    if meta.get("due"):
        try:
            due = date.fromisoformat(meta["due"])
        except ValueError:
            due = None
    return Followup(
        path=path,
        title=meta.get("title") or path.stem,
        account=meta.get("account") or "",
        due=due,
        status=(meta.get("status") or "open").lower(),
        created=meta.get("created"),
        body=body.strip(),
    )


def account_dir(account: str, root: Path = FOLLOWUPS_ROOT) -> Path:
    return root / slugify_name(account)


def iter_followups(account: str, *, root: Path = FOLLOWUPS_ROOT, include_done: bool = False) -> list[Followup]:
    base = account_dir(account, root)
    files: list[Path] = []
    if base.exists():
        files.extend(p for p in base.glob("*.md"))
        if include_done and (base / "done").exists():
            files.extend(p for p in (base / "done").glob("*.md"))
    return sorted((_parse(p) for p in files), key=lambda f: (f.due or date.max, f.title))


def due_followups(account: str, *, as_of: date, root: Path = FOLLOWUPS_ROOT) -> list[Followup]:
    """Open tasks whose due date is on or before ``as_of`` (the agent's check-in entry point)."""
    return [f for f in iter_followups(account, root=root) if f.status == "open" and f.due and f.due <= as_of]


def add_followup(
    *, account: str, title: str, due: str, note: str = "", created: str, root: Path = FOLLOWUPS_ROOT
) -> Path:
    base = account_dir(account, root)
    base.mkdir(parents=True, exist_ok=True)
    slug = slugify_name(title)[:50] or "task"
    path = base / f"{due}-{slug}.md"
    content = (
        "---\n"
        f"title: {title}\n"
        f"account: {slugify_name(account)}\n"
        f"due: {due}\n"
        "status: open\n"
        f"created: {created}\n"
        "---\n\n"
        f"{note}\n"
    )
    path.write_text(content, encoding="utf-8")
    return path


def mark_done(*, account: str, task_id: str, completed: str, root: Path = FOLLOWUPS_ROOT) -> Path:
    base = account_dir(account, root)
    src = base / f"{task_id}.md"
    if not src.exists():
        raise FileNotFoundError(f"Follow-up not found: {src}")
    text = src.read_text(encoding="utf-8")
    if "status:" in text:
        text = "\n".join(
            (f"status: done" if line.strip().startswith("status:") else line) for line in text.splitlines()
        )
    if "completed:" not in text:
        text = text.replace("status: done", f"status: done\ncompleted: {completed}", 1)
    done_dir = base / "done"
    done_dir.mkdir(parents=True, exist_ok=True)
    dest = done_dir / src.name
    dest.write_text(text, encoding="utf-8")
    src.unlink()
    return dest
