<script lang="ts">
	import type { TicketSummary } from '../lib/types.js';

	let { ticket }: { ticket: TicketSummary } = $props();

	function sequenceColor(s: number | null): string {
		if (s === null) return 'var(--text-light)';
		if (s <= 3) return 'var(--primary)';
		if (s <= 5) return 'var(--info, var(--primary))';
		if (s <= 8) return 'var(--text-muted)';
		return 'var(--text-light)';
	}
</script>

<a class="card" href="#/ticket/{ticket.stage}/{ticket.filename}">
	<div class="card-header">
		<span class="priority" style:color={sequenceColor(ticket.sequence)}>
			{ticket.sequence !== null ? `seq ${ticket.sequence}` : '—'}
		</span>
		<span class="slug">{ticket.slug}</span>
	</div>
	<div class="description">{ticket.description}</div>
	<div class="card-footer">
		{#if ticket.files?.length}
			<span class="file-count">{ticket.files.length} file{ticket.files.length !== 1 ? 's' : ''}</span>
		{/if}
	</div>
</a>

<style>
	.card {
		display: block;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 0.875rem 1rem;
		text-decoration: none;
		color: inherit;
		transition: all var(--transition);
	}
	.card:hover {
		border-color: var(--primary);
		box-shadow: var(--shadow-lg);
		transform: translateY(-1px);
		text-decoration: none;
	}
	.card-header {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		margin-bottom: 0.25rem;
	}
	.priority {
		font-size: 0.7rem;
		font-weight: 700;
		font-family: var(--font-mono);
	}
	.slug {
		font-weight: 600;
		font-size: 0.9rem;
		color: var(--text);
	}
	.description {
		font-size: 0.8rem;
		color: var(--text-muted);
		line-height: 1.4;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}
	.card-footer {
		display: flex;
		gap: 0.75rem;
		margin-top: 0.5rem;
	}
	.file-count {
		font-size: 0.7rem;
		color: var(--text-light);
	}
</style>
