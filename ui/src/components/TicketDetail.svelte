<script lang="ts">
	import { api } from '../lib/api.js';
	import { router } from '../lib/router.svelte.js';
	import type { TicketDetail } from '../lib/types.js';

	let { stage, filename }: { stage: string; filename: string } = $props();

	let ticket: TicketDetail | null = $state(null);
	let loading = $state(true);

	function sequenceColor(s: number | null): string {
		if (s === null) return 'var(--text-light)';
		if (s <= 3) return 'var(--primary)';
		if (s <= 5) return 'var(--info, var(--primary))';
		if (s <= 8) return 'var(--text-muted)';
		return 'var(--text-light)';
	}

	async function load() {
		loading = true;
		ticket = await api.ticket(stage, filename);
		loading = false;
	}

	$effect(() => { stage; filename; load(); });
</script>

{#if loading}
	<div class="loading">Loading...</div>
{:else if ticket}
	<div class="header">
		<button class="back" onclick={() => router.navigate(`/stage/${stage}`)}>← {stage}</button>
		<div class="header-info">
			<span class="priority" style:color={sequenceColor(ticket.sequence)}>seq {ticket.sequence ?? '—'}</span>
			<h1 class="title">{ticket.slug}</h1>
			<span class="stage-badge">{ticket.stage}</span>
		</div>
	</div>

	<div class="meta-bar">
		<div class="meta-item">
			<span class="meta-label">Description</span>
			<span class="meta-value">{ticket.description}</span>
		</div>
		{#if ticket.prereq}
			<div class="meta-item">
				<span class="meta-label">Prereq</span>
				<span class="meta-value">{ticket.prereq}</span>
			</div>
		{/if}
	</div>

	{#if ticket.files?.length}
		<div class="files-section">
			<h3 class="section-label">Files</h3>
			<div class="file-list">
				{#each ticket.files as file}
					<code class="file-path">{file}</code>
				{/each}
			</div>
		</div>
	{/if}

	<div class="body-section">
		<h3 class="section-label">Content</h3>
		<div class="body-content">
			<pre>{ticket.body}</pre>
		</div>
	</div>
{/if}

<style>
	.loading { text-align: center; padding: 3rem; color: var(--text-muted); }
	.header {
		display: flex;
		align-items: center;
		gap: 1rem;
		margin-bottom: 1rem;
	}
	.back {
		color: var(--text-muted);
		font-size: 0.875rem;
		padding: 0.375rem 0.75rem;
		border-radius: var(--radius);
		transition: all var(--transition);
		text-transform: capitalize;
	}
	.back:hover { background: var(--surface); color: var(--text); }
	.header-info {
		display: flex;
		align-items: baseline;
		gap: 0.75rem;
		flex: 1;
	}
	.priority {
		font-size: 0.8rem;
		font-weight: 700;
		font-family: var(--font-mono);
	}
	.title { font-size: 1.25rem; font-weight: 700; }
	.stage-badge {
		font-size: 0.65rem;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		padding: 0.125rem 0.5rem;
		border-radius: 99px;
		background: var(--primary-subtle);
		color: var(--primary);
	}

	.meta-bar {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 0.875rem 1rem;
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		margin-bottom: 1rem;
	}
	.meta-item {
		display: flex;
		gap: 0.75rem;
		align-items: baseline;
	}
	.meta-label {
		font-size: 0.7rem;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
		min-width: 90px;
	}
	.meta-value {
		font-size: 0.875rem;
		color: var(--text);
	}

	.section-label {
		font-size: 0.7rem;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
		margin-bottom: 0.5rem;
	}

	.files-section { margin-bottom: 1rem; }
	.file-list {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}
	.file-path {
		font-family: var(--font-mono);
		font-size: 0.8rem;
		color: var(--text-muted);
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 0.375rem 0.625rem;
	}

	.body-content {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 1.25rem;
		max-height: 70vh;
		overflow-y: auto;
	}
</style>
