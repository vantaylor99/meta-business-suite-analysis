<script lang="ts">
	import { api } from '../lib/api.js';
	import type { IndexStatus, IndexConfig, IndexJob } from '../lib/types.js';

	let status: IndexStatus | null = $state(null);
	let config: IndexConfig | null = $state(null);
	let job: IndexJob | null = $state(null);
	let loading = $state(true);
	let actionError: string | null = $state(null);
	let polling = false;

	const POLL_MS = 1500;

	async function loadStatus() {
		try {
			status = await api.indexStatus();
		} catch (err: any) {
			actionError = err.message;
		} finally {
			loading = false;
		}
	}

	async function loadConfig() {
		try { config = await api.indexConfig(); }
		catch { /* surfaced via top-level error if status also fails */ }
	}

	async function loadJob() {
		try {
			job = await api.indexJob();
		} catch { /* no job yet */ }
	}

	async function startRefresh() {
		actionError = null;
		try {
			job = await api.indexRefresh();
			beginPolling();
		} catch (err: any) {
			actionError = err.message;
		}
	}

	async function startRebuild() {
		if (!confirm('Rebuild drops the entire index and re-embeds every file. This can take several minutes. Continue?')) return;
		actionError = null;
		try {
			job = await api.indexRebuild();
			beginPolling();
		} catch (err: any) {
			actionError = err.message;
		}
	}

	function beginPolling() {
		if (polling) return;
		polling = true;
		const tick = async () => {
			await loadJob();
			if (job && job.status === 'running') {
				setTimeout(tick, POLL_MS);
			} else {
				polling = false;
				// One last status refresh once the job is done.
				await loadStatus();
			}
		};
		tick();
	}

	function fmtBytes(n: number | undefined): string {
		if (n === undefined) return '—';
		if (n < 1024) return `${n} B`;
		if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
		if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
		return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
	}

	function fmtAge(ms: number | undefined): string {
		if (ms === undefined) return '—';
		const age = Date.now() - ms;
		if (age < 60_000) return 'just now';
		if (age < 3_600_000) return `${Math.round(age / 60_000)} min ago`;
		if (age < 86_400_000) return `${Math.round(age / 3_600_000)} h ago`;
		return `${Math.round(age / 86_400_000)} d ago`;
	}

	function fmtNum(n: number | undefined): string {
		return n === undefined ? '—' : n.toLocaleString();
	}

	function fmtDuration(start: number, end: number | undefined): string {
		const ms = (end ?? Date.now()) - start;
		if (ms < 1000) return `${ms} ms`;
		if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
		return `${Math.round(ms / 60_000)} min ${Math.round((ms % 60_000) / 1000)} s`;
	}

	$effect(() => {
		loadStatus();
		loadConfig();
		loadJob().then(() => {
			if (job && job.status === 'running') beginPolling();
		});
	});
</script>

<div class="header">
	<h1 class="title">Code-search index</h1>
	<span class="caption">tickets/.index/index.db</span>
</div>

{#if loading}
	<div class="card loading">Loading index status…</div>
{:else if !status?.exists}
	<div class="card empty">
		<p><strong>No index yet.</strong></p>
		<p>Build it now to enable semantic and literal code search across the project.</p>
		<p class="hint">First-time builds download a ~155 MB embedding model (jina-embeddings-v2-base-code) and may take a few minutes; subsequent refreshes are incremental and usually sub-second.</p>
		<button class="primary" onclick={startRefresh} disabled={job?.status === 'running'}>
			Build index
		</button>
	</div>
{:else}
	<section class="card">
		<h2 class="section-title">Status</h2>
		<dl class="stats">
			<div><dt>Files</dt>           <dd>{fmtNum(status.files)}</dd></div>
			<div><dt>Chunks</dt>          <dd>{fmtNum(status.chunks)}</dd></div>
			<div><dt>DB size</dt>         <dd>{fmtBytes(status.dbSizeBytes)}</dd></div>
			<div><dt>Last modified</dt>   <dd>{fmtAge(status.dbModifiedMs)}</dd></div>
			<div><dt>Embedding model</dt> <dd class="mono">{status.modelId ?? '—'}</dd></div>
			<div><dt>Vector dim</dt>      <dd>{fmtNum(status.dim)}</dd></div>
			<div><dt>Schema version</dt>  <dd>{status.schemaVersion ?? '—'}</dd></div>
			<div class="wide"><dt>Path</dt> <dd class="mono">{status.dbPath}</dd></div>
		</dl>
	</section>

	<section class="card">
		<h2 class="section-title">Maintenance</h2>
		<div class="actions">
			<button class="primary" onclick={startRefresh} disabled={job?.status === 'running'}>
				Incremental refresh
			</button>
			<button class="danger" onclick={startRebuild} disabled={job?.status === 'running'}>
				Full rebuild
			</button>
			<p class="hint">
				Refresh re-embeds only files whose content hash changed (typically sub-second). Rebuild drops the DB and re-embeds everything — only needed after a model swap or schema-version bump.
			</p>
		</div>
		{#if actionError}
			<div class="error">{actionError}</div>
		{/if}
	</section>
{/if}

{#if config}
	<section class="card">
		<h2 class="section-title">Configuration</h2>
		{#if config.source}
			<p class="config-source">
				Project config: <code class="mono">{config.source}</code>
			</p>
		{:else}
			<p class="config-source muted">
				No project config — using defaults only. Create
				<code class="mono">tickets/index-config.json</code> to customize.
			</p>
		{/if}
		<div class="config-grid">
			<div class="config-block">
				<dt>Excluded directories ({config.effective.exclude.length})</dt>
				<dd>
					{#each config.defaults.exclude as p}<span class="chip default" title="default">{p}</span>{/each}
					{#each config.project.exclude as p}<span class="chip project" title="project config">{p}</span>{/each}
				</dd>
			</div>
			{#if config.effective.include.length > 0}
				<div class="config-block">
					<dt>Re-included paths ({config.effective.include.length})</dt>
					<dd>
						{#each config.project.include as p}<span class="chip include" title="project config">{p}</span>{/each}
					</dd>
				</div>
			{/if}
			<div class="config-block">
				<dt>Extensions ({config.effective.extensions.length})</dt>
				<dd>
					{#each config.defaults.extensions as e}<span class="chip default mono">{e}</span>{/each}
					{#each config.project.extensions as e}<span class="chip project mono">{e}</span>{/each}
				</dd>
			</div>
		</div>
		<p class="legend">
			<span class="chip default sample">default</span>
			<span class="chip project sample">project config</span>
			<span class="chip include sample">re-include</span>
			· Edits take effect on the next refresh / rebuild.
		</p>
	</section>
{/if}

{#if job}
	<section class="card job">
		<h2 class="section-title">
			Last job — <span class="job-kind">{job.kind}</span>
			<span class="job-status status-{job.status}">{job.status}</span>
			<span class="job-meta">{fmtDuration(job.startedAt, job.endedAt)}</span>
			{#if job.exitCode !== undefined && job.exitCode !== 0}
				<span class="job-meta error-text">exit {job.exitCode}</span>
			{/if}
		</h2>
		<pre class="log"><code>{job.logTail.join('\n') || '(no output yet)'}</code></pre>
	</section>
{/if}

<style>
	.header {
		display: flex;
		align-items: baseline;
		gap: 1rem;
		margin-bottom: 1rem;
	}
	.title { font-size: 1.25rem; font-weight: 700; }
	.caption {
		font-family: var(--font-mono);
		font-size: 0.8rem;
		color: var(--text-muted);
	}

	.card {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 1rem 1.25rem;
		margin-bottom: 1rem;
	}
	.section-title {
		font-size: 0.75rem;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--text-muted);
		margin-bottom: 0.75rem;
		display: flex;
		align-items: center;
		gap: 0.5rem;
		flex-wrap: wrap;
	}

	.stats {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
		gap: 0.75rem 1.25rem;
	}
	.stats > div { display: flex; flex-direction: column; gap: 0.125rem; }
	.stats > div.wide { grid-column: 1 / -1; }
	.stats dt {
		font-size: 0.7rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-muted);
	}
	.stats dd {
		font-size: 0.95rem;
		font-weight: 600;
		color: var(--text);
	}
	.stats dd.mono {
		font-family: var(--font-mono);
		font-size: 0.825rem;
		font-weight: 500;
		word-break: break-all;
	}

	.actions {
		display: flex;
		gap: 0.75rem;
		align-items: center;
		flex-wrap: wrap;
	}
	.actions .hint {
		flex: 1 1 100%;
		font-size: 0.8rem;
		color: var(--text-muted);
		line-height: 1.5;
	}
	button.primary, button.danger {
		font-weight: 600;
		font-size: 0.875rem;
		padding: 0.55rem 1.25rem;
		border-radius: var(--radius);
		transition: background var(--transition);
	}
	button.primary { background: var(--primary); color: var(--on-primary); }
	button.primary:hover:not(:disabled) { background: var(--primary-hover); }
	button.danger { background: var(--danger-subtle); color: var(--danger); border: 1px solid var(--danger); }
	button.danger:hover:not(:disabled) { background: var(--danger); color: var(--on-primary); }
	button:disabled { opacity: 0.5; cursor: not-allowed; }

	.error {
		margin-top: 0.75rem;
		padding: 0.5rem 0.75rem;
		background: var(--danger-subtle);
		border: 1px solid var(--danger);
		border-radius: var(--radius);
		color: var(--danger);
		font-size: 0.85rem;
	}

	.job-kind {
		font-family: var(--font-mono);
		font-size: 0.8rem;
		color: var(--text);
		text-transform: none;
		letter-spacing: 0;
	}
	.job-status {
		font-size: 0.7rem;
		font-weight: 700;
		padding: 0.125rem 0.5rem;
		border-radius: 999px;
		text-transform: uppercase;
	}
	.job-status.status-running { background: var(--info-subtle); color: var(--info); }
	.job-status.status-success { background: var(--success-subtle); color: var(--success); }
	.job-status.status-error   { background: var(--danger-subtle);  color: var(--danger); }
	.job-meta {
		font-family: var(--font-mono);
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--text-muted);
		text-transform: none;
		letter-spacing: 0;
	}
	.error-text { color: var(--danger); }

	.log {
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		font-family: var(--font-mono);
		font-size: 0.78rem;
		line-height: 1.5;
		padding: 0.75rem;
		max-height: 320px;
		overflow: auto;
		white-space: pre;
	}

	.config-source {
		font-size: 0.85rem;
		margin-bottom: 0.75rem;
		color: var(--text);
	}
	.config-source.muted { color: var(--text-muted); }
	.config-source code.mono {
		font-family: var(--font-mono);
		font-size: 0.85em;
		background: var(--bg);
		padding: 0.125rem 0.375rem;
		border-radius: 4px;
	}

	.config-grid {
		display: flex;
		flex-direction: column;
		gap: 0.875rem;
	}
	.config-block dt {
		font-size: 0.7rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-muted);
		margin-bottom: 0.375rem;
	}
	.config-block dd {
		display: flex;
		flex-wrap: wrap;
		gap: 0.25rem;
	}

	.chip {
		display: inline-block;
		font-size: 0.75rem;
		font-weight: 500;
		padding: 0.125rem 0.5rem;
		border-radius: 999px;
		border: 1px solid var(--border);
	}
	.chip.mono { font-family: var(--font-mono); }
	.chip.default { background: var(--surface-raised); color: var(--text-muted); }
	.chip.project { background: var(--info-subtle); color: var(--info); border-color: var(--info); }
	.chip.include { background: var(--success-subtle); color: var(--success); border-color: var(--success); }
	.chip.sample { font-family: var(--font); }

	.legend {
		margin-top: 0.875rem;
		padding-top: 0.75rem;
		border-top: 1px solid var(--border);
		font-size: 0.75rem;
		color: var(--text-muted);
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.375rem;
	}

	.empty, .loading {
		text-align: center;
		padding: 2rem 1.5rem;
	}
	.empty p { margin-bottom: 0.5rem; color: var(--text); }
	.empty p.hint {
		color: var(--text-muted);
		font-size: 0.85rem;
		max-width: 480px;
		margin: 0.75rem auto 1rem;
	}
</style>
