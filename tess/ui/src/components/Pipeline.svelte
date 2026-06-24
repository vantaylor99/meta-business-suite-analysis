<script lang="ts">
	import { api } from '../lib/api.js';
	import type { PipelineCounts } from '../lib/types.js';

	let counts: PipelineCounts | null = $state(null);
	let loading = $state(true);

	const stages = [
		{ key: 'backlog', label: 'Backlog', color: 'var(--text-muted)', desc: 'Parked specs' },
		{ key: 'fix', label: 'Fix', color: 'var(--danger)', desc: 'Bug triage' },
		{ key: 'plan', label: 'Plan', color: 'var(--warning)', desc: 'Feature design' },
		{ key: 'implement', label: 'Implement', color: 'var(--primary)', desc: 'Build & test' },
		{ key: 'review', label: 'Review', color: 'var(--info)', desc: 'Code review' },
		{ key: 'blocked', label: 'Blocked', color: 'var(--danger)', desc: 'Unresolved' },
		{ key: 'complete', label: 'Complete', color: 'var(--success)', desc: 'Archived' },
	] as const;

	const sideKeys = new Set(['backlog', 'blocked', 'complete']);
	const sideOrder = ['backlog', 'blocked', 'complete'];
	const activeStages = stages.filter(s => !sideKeys.has(s.key));
	const sideStages = sideOrder
		.map(k => stages.find(s => s.key === k)!)
		.filter(Boolean);

	const total = $derived(counts ? Object.values(counts).reduce((a, b) => a + b, 0) : 0);

	async function load() {
		loading = true;
		counts = await api.pipeline();
		loading = false;
	}

	$effect(() => { load(); });
</script>

{#if loading}
	<div class="loading">Loading pipeline...</div>
{:else if counts}
	<h2 class="page-title">Ticket Pipeline</h2>

	<div class="flow">
		{#each activeStages as stage, i}
			{@const count = counts[stage.key] ?? 0}
			{#if i > 0}
				<span class="flow-arrow">→</span>
			{/if}
			<a class="stage-card" href="#/stage/{stage.key}" class:has-items={count > 0}>
				<div class="stage-count" style:color={count > 0 ? stage.color : 'var(--text-light)'}>
					{count}
				</div>
				<div class="stage-label">{stage.label}</div>
				<div class="stage-desc">{stage.desc}</div>
			</a>
		{/each}
	</div>

	<div class="side-stages">
		{#each sideStages as stage}
			{@const count = counts[stage.key] ?? 0}
			<a class="side-card" href="#/stage/{stage.key}" class:has-items={count > 0}>
				<div class="side-count" style:color={count > 0 ? stage.color : 'var(--text-light)'}>
					{count}
				</div>
				<div class="side-info">
					<div class="side-label">{stage.label}</div>
					<div class="side-desc">{stage.desc}</div>
				</div>
			</a>
		{/each}
		<div class="total-card">
			<span class="total-count">{total}</span>
			<span class="total-label">total tickets</span>
		</div>
	</div>
{/if}

<style>
	.loading { text-align: center; padding: 3rem; color: var(--text-muted); }
	.page-title {
		font-size: 0.8rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-muted);
		margin-bottom: 1rem;
	}

	.flow {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		margin-bottom: 1rem;
		flex-wrap: wrap;
	}
	.flow-arrow {
		color: var(--text-light);
		font-size: 1.5rem;
	}
	.stage-card {
		flex: 1;
		min-width: 120px;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 1.25rem 1rem;
		text-align: center;
		text-decoration: none;
		color: inherit;
		transition: all var(--transition);
	}
	.stage-card:hover {
		border-color: var(--primary);
		box-shadow: var(--shadow-lg);
		transform: translateY(-1px);
		text-decoration: none;
	}
	.stage-count {
		font-size: 2rem;
		font-weight: 700;
		line-height: 1.2;
	}
	.stage-label {
		font-size: 0.8rem;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text);
		margin-top: 0.25rem;
	}
	.stage-desc {
		font-size: 0.7rem;
		color: var(--text-muted);
		margin-top: 0.125rem;
	}

	.side-stages {
		display: flex;
		gap: 0.75rem;
		flex-wrap: wrap;
	}
	.side-card {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 0.75rem 1rem;
		text-decoration: none;
		color: inherit;
		transition: all var(--transition);
		flex: 1;
		min-width: 160px;
	}
	.side-card:hover {
		border-color: var(--primary);
		text-decoration: none;
	}
	.side-count {
		font-size: 1.5rem;
		font-weight: 700;
		line-height: 1;
	}
	.side-label {
		font-weight: 600;
		font-size: 0.85rem;
	}
	.side-desc {
		font-size: 0.7rem;
		color: var(--text-muted);
	}
	.total-card {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		margin-left: auto;
		padding: 0.75rem 1rem;
	}
	.total-count {
		font-size: 1.5rem;
		font-weight: 700;
		color: var(--text);
	}
	.total-label {
		font-size: 0.7rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}
</style>
