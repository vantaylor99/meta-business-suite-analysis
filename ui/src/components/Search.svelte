<script lang="ts">
	import { api } from '../lib/api.js';
	import type { SearchResults, RefResults, ChunkResult, SearchMatch, RefMatch } from '../lib/types.js';

	type Mode = 'semantic' | 'refs';

	let mode: Mode = $state('semantic');
	let query = $state('');
	let pathFilter = $state('');
	let k = $state(5);
	let max = $state(50);

	let loading = $state(false);
	let error: string | null = $state(null);
	let indexMissing = $state(false);
	let semantic: SearchResults | null = $state(null);
	let refs: RefResults | null = $state(null);
	let elapsed = $state(0);

	// Heuristic that mirrors the agent-rules guidance: a "bag of identifiers"
	// (no spaces, or pipe-separated identifier-like tokens) is the wrong shape
	// for semantic search.  We do NOT auto-switch — surface the hint and let
	// the user decide.
	const looksIdentifier = $derived.by(() => {
		const q = query.trim();
		if (!q) return false;
		if (q.includes(' ')) return false;
		return /^[A-Za-z_$][\w$|.]*$/.test(q);
	});

	const semanticHint = $derived(mode === 'semantic' && looksIdentifier);

	async function run(e?: Event) {
		e?.preventDefault();
		const q = query.trim();
		if (!q) return;
		loading = true;
		error = null;
		indexMissing = false;
		semantic = null;
		refs = null;
		const started = performance.now();
		try {
			const filter = pathFilter.trim() || null;
			if (mode === 'semantic') {
				semantic = await api.search(q, { k, pathFilter: filter });
			} else {
				refs = await api.refs(q, { max, pathFilter: filter });
			}
		} catch (err: any) {
			if (err.code === 'index-not-built' || err.status === 503) {
				indexMissing = true;
			} else {
				error = err.message;
			}
		} finally {
			elapsed = performance.now() - started;
			loading = false;
		}
	}

	function topScore(matches: SearchMatch[]): number {
		return matches.length > 0 ? matches[0].score : 0;
	}
	function relPercent(score: number, top: number): number {
		if (top <= 0) return 0;
		return Math.round((score / top) * 100);
	}
</script>

<div class="header">
	<h1 class="title">Code search</h1>
	<span class="caption">Local index over <code>git ls-files</code></span>
</div>

<form class="search-form" onsubmit={run}>
	<div class="mode-toggle" role="tablist">
		<button
			type="button"
			class="mode-btn"
			class:active={mode === 'semantic'}
			onclick={() => (mode = 'semantic')}
			role="tab"
			aria-selected={mode === 'semantic'}
		>
			Semantic
		</button>
		<button
			type="button"
			class="mode-btn"
			class:active={mode === 'refs'}
			onclick={() => (mode = 'refs')}
			role="tab"
			aria-selected={mode === 'refs'}
		>
			Literal (refs)
		</button>
	</div>

	<input
		class="query"
		type="search"
		bind:value={query}
		placeholder={mode === 'semantic'
			? 'e.g. "where do we evict pages from the buffer pool"'
			: 'e.g. composeNewSlot|defaultComposeNewSlot'}
		autocomplete="off"
		spellcheck="false"
	/>

	<div class="controls">
		<label>
			<span>Path filter</span>
			<input
				type="text"
				bind:value={pathFilter}
				placeholder="packages/lamina-substrate/%"
				spellcheck="false"
			/>
		</label>
		{#if mode === 'semantic'}
			<label class="num">
				<span>k</span>
				<input type="number" min="1" max="50" bind:value={k} />
			</label>
		{:else}
			<label class="num">
				<span>max</span>
				<input type="number" min="1" max="500" bind:value={max} />
			</label>
		{/if}
		<button class="run" type="submit" disabled={loading || !query.trim()}>
			{loading ? 'Searching…' : 'Search'}
		</button>
	</div>

	{#if semanticHint}
		<div class="hint warn">
			Identifier-shaped query — <button type="button" class="linkish" onclick={() => (mode = 'refs')}>switch to literal (refs)</button>
			for an exact-match search. Semantic search embeds the query as natural language and tends to score noise on bare identifiers.
		</div>
	{/if}
</form>

{#if indexMissing}
	<div class="empty">
		<p><strong>No index found.</strong></p>
		<p>Build it with <code>node tess/scripts/index.mjs</code> from the project root, or visit the <a href="#/index">Index page</a> to do it from here.</p>
	</div>
{:else if error}
	<div class="error">Error: {error}</div>
{:else if loading}
	<div class="loading">Searching…</div>
{:else if semantic}
	<div class="results-meta">
		{semantic.matches.length} match{semantic.matches.length !== 1 ? 'es' : ''}
		· {elapsed.toFixed(0)} ms
		{#if semantic.matches.length > 0 && topScore(semantic.matches) < 0.05}
			· <span class="warn-inline">weak top score (raw cosine {topScore(semantic.matches).toFixed(3)}) — may be noise</span>
		{/if}
	</div>
	{#if semantic.matches.length === 0}
		<div class="empty">No matches.</div>
	{:else}
		{@const top = topScore(semantic.matches)}
		{#each semantic.matches as m, i}
			<article class="match">
				<header class="match-head">
					<span class="match-rank">[{i + 1}]</span>
					{#if i === 0}
						<span class="badge top">top match</span>
					{:else if top > 0}
						<span class="badge rel">{relPercent(m.score, top)}% of top</span>
					{/if}
					<code class="match-path">{m.path}:{m.start_line}-{m.end_line}</code>
					<span class="match-score" title="raw cosine similarity">{m.score.toFixed(3)}</span>
				</header>
				<pre class="snippet"><code>{m.text}</code></pre>
			</article>
		{/each}
	{/if}
{:else if refs}
	<div class="results-meta">
		{refs.rows.length} hit{refs.rows.length !== 1 ? 's' : ''}
		· {elapsed.toFixed(0)} ms
		{#if refs.rows.length === refs.max}
			· <span class="warn-inline">capped at max={refs.max} — raise to see more</span>
		{/if}
	</div>
	{#if refs.rows.length === 0}
		<div class="empty">No matches for <code>{refs.symbol}</code>.</div>
	{:else}
		{#each refs.rows as r}
			<article class="match">
				<header class="match-head">
					<code class="match-path">{r.path}:{r.start_line}-{r.end_line}</code>
				</header>
				<pre class="snippet"><code>{r.text}</code></pre>
			</article>
		{/each}
	{/if}
{:else}
	<div class="hint">
		<strong>Decision rule:</strong> identifier-shaped query → use Literal; prose → use Semantic.
		<code>search_code</code> embeds the query as natural language, so a list of symbols collapses to noise.
	</div>
{/if}

<style>
	.header {
		display: flex;
		align-items: baseline;
		gap: 1rem;
		margin-bottom: 1rem;
	}
	.title { font-size: 1.25rem; font-weight: 700; }
	.caption { font-size: 0.8rem; color: var(--text-muted); }

	.search-form {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 1rem;
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
		margin-bottom: 1rem;
	}
	.mode-toggle {
		display: inline-flex;
		gap: 0.25rem;
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 0.25rem;
		align-self: flex-start;
	}
	.mode-btn {
		padding: 0.375rem 0.875rem;
		border-radius: calc(var(--radius) - 2px);
		font-size: 0.8rem;
		font-weight: 600;
		color: var(--text-muted);
		transition: all var(--transition);
	}
	.mode-btn:hover { color: var(--text); }
	.mode-btn.active {
		background: var(--surface);
		color: var(--primary);
		box-shadow: var(--shadow);
	}

	.query {
		width: 100%;
		font-family: var(--font-mono);
		font-size: 0.95rem;
		padding: 0.625rem 0.875rem;
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		color: var(--text);
		transition: border-color var(--transition);
	}
	.query:focus { outline: none; border-color: var(--primary); }

	.controls {
		display: flex;
		gap: 0.75rem;
		align-items: flex-end;
		flex-wrap: wrap;
	}
	.controls label {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		flex: 1;
		min-width: 180px;
	}
	.controls label.num {
		flex: 0;
		min-width: 80px;
	}
	.controls label > span {
		font-size: 0.7rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-muted);
	}
	.controls input {
		font-family: var(--font-mono);
		font-size: 0.85rem;
		padding: 0.5rem 0.75rem;
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		color: var(--text);
	}
	.controls input:focus { outline: none; border-color: var(--primary); }
	.run {
		background: var(--primary);
		color: var(--on-primary);
		font-weight: 600;
		font-size: 0.875rem;
		padding: 0.55rem 1.25rem;
		border-radius: var(--radius);
		transition: background var(--transition);
	}
	.run:hover:not(:disabled) { background: var(--primary-hover); }
	.run:disabled { opacity: 0.5; cursor: not-allowed; }

	.hint {
		font-size: 0.825rem;
		color: var(--text-muted);
		background: var(--surface-raised);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 0.625rem 0.875rem;
		line-height: 1.5;
	}
	.hint code { font-family: var(--font-mono); font-size: 0.85em; }
	.hint.warn {
		background: var(--warning-subtle);
		border-color: var(--warning);
		color: var(--text);
	}
	.linkish {
		color: var(--primary);
		text-decoration: underline;
		font-weight: 600;
	}

	.results-meta {
		font-size: 0.8rem;
		color: var(--text-muted);
		margin-bottom: 0.75rem;
	}
	.warn-inline { color: var(--warning); font-weight: 600; }

	.match {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		margin-bottom: 0.75rem;
		overflow: hidden;
	}
	.match-head {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.5rem 0.75rem;
		background: var(--surface-raised);
		border-bottom: 1px solid var(--border);
		font-size: 0.8rem;
		flex-wrap: wrap;
	}
	.match-rank { color: var(--text-muted); font-family: var(--font-mono); }
	.match-path {
		font-family: var(--font-mono);
		color: var(--text);
		flex: 1;
		min-width: 0;
		word-break: break-all;
	}
	.match-score {
		font-family: var(--font-mono);
		font-size: 0.75rem;
		color: var(--text-muted);
	}
	.badge {
		font-size: 0.7rem;
		font-weight: 700;
		padding: 0.125rem 0.5rem;
		border-radius: 999px;
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
	.badge.top { background: var(--success-subtle); color: var(--success); }
	.badge.rel { background: var(--info-subtle); color: var(--info); }

	.snippet {
		font-family: var(--font-mono);
		font-size: 0.8rem;
		line-height: 1.5;
		padding: 0.75rem;
		overflow-x: auto;
		max-height: 400px;
		overflow-y: auto;
		background: var(--bg);
	}
	.snippet code { font-family: inherit; white-space: pre; }

	.loading, .empty, .error {
		text-align: center;
		padding: 2rem;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		color: var(--text-muted);
	}
	.empty p { margin-bottom: 0.5rem; }
	.empty code { font-family: var(--font-mono); font-size: 0.85em; }
	.error { color: var(--danger); border-color: var(--danger); background: var(--danger-subtle); }
</style>
