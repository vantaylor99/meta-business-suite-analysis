## Code search (tess)

**First tool** for any "where / how / why" question about this codebase: the local code-aware index wired to `mcp__code-search__*`. Reach for `grep`/`Glob` only when you already know the exact filename or literal string. Pick the right sub-tool — they are not interchangeable.

**Decision rule:**

- Query is identifier-shaped (any single symbol, camelCase, snake_case, or a list of names like `fooBar bazQux`)? → `find_references`.
- Query is prose ("where do we evict pages", "what handles JWT refresh", you don't yet know the identifier)? → `search_code`.
- About to run more than one `grep` to reconstruct context? → run `search_code` first instead. That is the moment it pays off, even when you already know an identifier.

`search_code` embeds the query as natural language. Identifier-bag queries can still work when the identifiers co-locate in real code, but prose phrasing is more reliable. If `search_code` returns a weak-top warning, the relative-percentage ranking is unreliable — switch to `find_references` or rephrase as prose, do **not** trust the ordering on noisy results.

**Tools:**

- `search_code(query, k?, path_filter?)` — semantic search. Scores are relative within each result set, not absolute. `k` defaults to 5 (max 50) — raise it for broad sweeps, lower it when you know the top hit is enough. `path_filter` is a SQL LIKE pattern, e.g. `"packages/lamina/%"`.
- `find_references(symbol, max?, path_filter?)` — literal substring; `|` ORs alternatives (`Foo|Bar`). Returns every hit (capped by `max`, default 50, max 500). This is the indexed replacement for `grep` on identifiers.
- `read_chunk(path, start_line, end_line)` — expand a snippet from either tool without a separate `Read`.

**Fallbacks:**

- Use `grep`/`Glob` only for filename patterns, regex with anchors/lookarounds, or when you need *every* literal hit (the index is chunk-granular and may miss adjacent matches inside one chunk).
- Never fall back to `grep` when `find_references` would suffice — it's strictly slower and pulls more bytes.

**What's indexed:** project source files tracked by git, minus `node_modules/`, `dist/`, `build/`, `.git/`, `tickets/`, `team/`, `docs/`, and a few cache dirs. If a query about prose-heavy material (long-form architecture docs, design notes, READMEs in nested folders) returns nothing, the file may be outside the indexed set — fall back to `Read`/`Glob` for those paths. Projects can override the filter via `tickets/index-config.json` (see tess README § Customize what gets indexed).
