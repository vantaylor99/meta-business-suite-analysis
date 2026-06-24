<script lang="ts">
	import { router } from './lib/router.svelte.js';
	import { api } from './lib/api.js';
	import type { SiblingInfo } from './lib/types.js';
	import Pipeline from './components/Pipeline.svelte';
	import StageView from './components/StageView.svelte';
	import TicketDetail from './components/TicketDetail.svelte';
	import Search from './components/Search.svelte';
	import IndexStatus from './components/IndexStatus.svelte';

	let sibling: SiblingInfo | null = $state(null);

	const stageMatch = $derived(router.match('/stage/:name'));
	const ticketMatch = $derived(router.match('/ticket/:stage/:filename'));
	const isSearch = $derived(router.path === '/search');
	const isIndex = $derived(router.path === '/index');

	$effect(() => {
		api.sibling().then(s => sibling = s).catch(() => {});
	});
</script>

<nav class="nav">
	<a class="nav-brand" href="#/">Tess</a>
	<div class="nav-links">
		<a class="nav-link" class:active={router.path === '/'} href="#/">Pipeline</a>
		<a class="nav-link" class:active={isSearch} href="#/search">Search</a>
		<a class="nav-link" class:active={isIndex} href="#/index">Index</a>
	</div>
	{#if sibling}
		<a class="sibling-link" href={sibling.url}>
			{sibling.name} →
		</a>
	{/if}
</nav>

<main class="main">
	{#if ticketMatch}
		<TicketDetail stage={ticketMatch.stage} filename={ticketMatch.filename} />
	{:else if stageMatch}
		<StageView stage={stageMatch.name} />
	{:else if isSearch}
		<Search />
	{:else if isIndex}
		<IndexStatus />
	{:else}
		<Pipeline />
	{/if}
</main>

<style>
	.nav {
		display: flex;
		align-items: center;
		gap: 2rem;
		padding: 0 1.5rem;
		height: 56px;
		background: var(--surface);
		border-bottom: 1px solid var(--border);
		box-shadow: var(--shadow);
		position: sticky;
		top: 0;
		z-index: 100;
	}
	.nav-brand {
		font-weight: 700;
		font-size: 1.125rem;
		color: var(--text);
		letter-spacing: -0.02em;
	}
	.nav-brand:hover { text-decoration: none; }
	.nav-links { display: flex; gap: 0.25rem; flex: 1; }
	.nav-link {
		padding: 0.375rem 0.75rem;
		border-radius: var(--radius);
		color: var(--text-muted);
		font-weight: 500;
		font-size: 0.875rem;
		transition: all var(--transition);
	}
	.nav-link:hover { background: var(--bg); color: var(--text); text-decoration: none; }
	.nav-link.active { background: var(--primary-subtle); color: var(--primary); }
	.sibling-link {
		font-size: 0.8rem;
		font-weight: 600;
		padding: 0.375rem 0.75rem;
		border: 1px solid var(--border);
		border-radius: var(--radius);
		color: var(--text-muted);
		transition: all var(--transition);
	}
	.sibling-link:hover {
		border-color: var(--primary);
		color: var(--primary);
		text-decoration: none;
	}
	.main {
		max-width: 1200px;
		margin: 0 auto;
		padding: 1.5rem;
	}
</style>
