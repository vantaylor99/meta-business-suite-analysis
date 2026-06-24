import type {
	PipelineCounts,
	TicketSummary,
	TicketDetail,
	SiblingInfo,
	SearchResults,
	RefResults,
	ChunkResult,
	IndexStatus,
	IndexConfig,
	IndexJob,
} from './types.js';

async function get<T>(url: string): Promise<T> {
	const res = await fetch(url);
	if (!res.ok) throw await asError(res);
	return res.json();
}

async function post<T>(url: string): Promise<T> {
	const res = await fetch(url, { method: 'POST' });
	if (!res.ok) throw await asError(res);
	return res.json();
}

async function asError(res: Response): Promise<Error> {
	let body: any = null;
	try { body = await res.json(); } catch { /* not json */ }
	const msg = body?.message ?? body?.error ?? `${res.status} ${res.statusText}`;
	const err = new Error(msg) as Error & { status: number; code?: string };
	err.status = res.status;
	if (body?.error) err.code = body.error;
	return err;
}

function qs(params: Record<string, string | number | null | undefined>): string {
	const u = new URLSearchParams();
	for (const [k, v] of Object.entries(params)) {
		if (v === null || v === undefined || v === '') continue;
		u.set(k, String(v));
	}
	const s = u.toString();
	return s ? `?${s}` : '';
}

export const api = {
	pipeline: () => get<PipelineCounts>('/api/pipeline'),
	stage: (name: string) => get<TicketSummary[]>(`/api/stages/${encodeURIComponent(name)}`),
	ticket: (stage: string, filename: string) =>
		get<TicketDetail>(`/api/tickets/${encodeURIComponent(stage)}/${encodeURIComponent(filename)}`),
	sibling: () => get<SiblingInfo | null>('/api/sibling'),

	search: (q: string, opts: { k?: number; pathFilter?: string | null } = {}) =>
		get<SearchResults>('/api/search' + qs({ q, k: opts.k, path: opts.pathFilter ?? null })),

	refs: (q: string, opts: { max?: number; pathFilter?: string | null } = {}) =>
		get<RefResults>('/api/refs' + qs({ q, max: opts.max, path: opts.pathFilter ?? null })),

	chunk: (path: string, start: number, end: number) =>
		get<ChunkResult>('/api/chunk' + qs({ path, start, end })),

	indexStatus: () => get<IndexStatus>('/api/index/status'),
	indexConfig: () => get<IndexConfig>('/api/index/config'),
	indexJob: () => get<IndexJob | null>('/api/index/job'),
	indexRefresh: () => post<IndexJob>('/api/index/refresh'),
	indexRebuild: () => post<IndexJob>('/api/index/rebuild'),
};
