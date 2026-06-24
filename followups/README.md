# Follow-ups — account to-do items with due dates

This is the project's lightweight, **due-date-aware** to-do list for ongoing ad-account work
(e.g. "evaluate the new ad after a week and prune to the best copy"). It is **separate from**
agent-created Jira / `calls` tickets — those track software work; these track account-management
follow-ups a human or agent should act on later.

## Why it exists (context efficiency)

When checking in on an account, an agent should **not** read every task file. Instead it runs one
command that surfaces **only what's due**:

```
python -m meta_ads_analysis followups due --account <slug>
```

That prints just the title + due date + id of due/overdue tasks. The agent reads a task's body
**only when it's due**, acts on it, then marks it done. Future-dated tasks stay out of context.

## Layout

```
followups/<account_slug>/<due>-<slug>.md     # open tasks (frontmatter: title, account, due, status, created)
followups/<account_slug>/done/<...>.md        # completed (archived)
```

## Commands

- `followups due --account <slug> [--as-of YYYY-MM-DD]` — list due/overdue (the check-in entry point).
- `followups list --account <slug> [--all]` — list open tasks (with `--all`, include done).
- `followups add --account <slug> --title "..." --due YYYY-MM-DD [--note "..."]` — add a task.
- `followups done --account <slug> <id>` — mark a task done (archives it).

## Convention

The **first step of any account check-in is `followups due`** (see `knowledge/README.md`). Record a
follow-up whenever a decision implies "check back / do this later" (a learning-phase wait, a prune,
a budget review, a creative refresh).
