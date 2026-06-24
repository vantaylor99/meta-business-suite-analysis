<script lang="ts">
	import { api } from '../lib/api.js';
	import { router } from '../lib/router.svelte.js';
	import type { TicketSummary } from '../lib/types.js';
	import TicketCard from './TicketCard.svelte';

	let { stage }: { stage: string } = $props();

	let tickets: TicketSummary[] = $state([]);
	let loading = $state(true);

	const stageLabels: Record<string, string> = {
		backlog: 'Backlog — Parked Specs',
		fix: 'Fix — Bug Triage',
		plan: 'Plan — Feature Design',
		implement: 'Implement — Build & Test',
		review: 'Review — Code Review',
		blocked: 'Blocked — Unresolved',
		complete: 'Complete — Archived',
	};

	async function load() {
		loading = true;
		tickets = await api.stage(stage);
		loading = false;
	}

	$effect(() => { stage; load(); });
</script>

<div class="header">
	<button class="back" onclick={() => router.navigate('/')}>← Pipeline</button>
	<h1 class="title">{stageLabels[stage] ?? stage}</h1>
	<span class="count">{tickets.length} ticket{tickets.length !== 1 ? 's' : ''}</span>
</div>

{#if loading}
	<div class="loading">Loading...</div>
{:else if tickets.length === 0}
	<div class="empty">No tickets in {stage}</div>
{:else}
	<div class="ticket-list">
		{#each tickets as ticket}
			<TicketCard {ticket} />
		{/each}
	</div>
{/if}

<style>
	.header {
		display: flex;
		align-items: center;
		gap: 1rem;
		margin-bottom: 1.25rem;
	}
	.back {
		color: var(--text-muted);
		font-size: 0.875rem;
		padding: 0.375rem 0.75rem;
		border-radius: var(--radius);
		transition: all var(--transition);
	}
	.back:hover { background: var(--surface); color: var(--text); }
	.title { font-size: 1.25rem; font-weight: 700; }
	.count {
		font-size: 0.8rem;
		color: var(--text-muted);
		margin-left: auto;
	}
	.loading { text-align: center; padding: 3rem; color: var(--text-muted); }
	.empty {
		text-align: center;
		padding: 3rem;
		color: var(--text-muted);
		font-style: italic;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: var(--radius);
	}
	.ticket-list {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}
</style>
