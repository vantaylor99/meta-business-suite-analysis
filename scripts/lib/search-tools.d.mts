/**
 * Type declarations for search-tools.mjs.
 *
 * Authoritative source is the .mjs; this file exists so TypeScript consumers
 * (notably tess/ui under bundler module resolution) can type-check imports
 * without us migrating the runtime to .ts.
 */

export interface SearchCtx {
	store: {
		dim: number;
		stats(): { files: number; chunks: number };
		getMeta(key: string): string | null;
		setMeta(key: string, value: string): void;
		close(): void;
	};
	ensureEmbedder: () => Promise<unknown>;
	indexDir: string;
	dbPath: string;
	repoRoot: string;
}

export interface SearchMatch {
	path: string;
	start_line: number;
	end_line: number;
	text: string;
	score: number;
}

export interface RefRow {
	path: string;
	start_line: number;
	end_line: number;
	text: string;
}

export interface ChunkResult {
	path: string;
	start: number;
	end: number;
	text: string;
}

export class IndexNotBuiltError extends Error {
	code: 'INDEX_NOT_BUILT';
	dbPath: string;
}

export function openSearchIndex(opts: { repoRoot: string }): Promise<SearchCtx>;

export function searchCode(
	args: { query: string; k?: number; pathFilter?: string | null },
	ctx: SearchCtx,
): Promise<SearchMatch[]>;

export function findReferences(
	args: { symbol: string; max?: number; pathFilter?: string | null },
	ctx: SearchCtx,
): RefRow[];

export function readChunk(
	args: { path: string; startLine: number; endLine: number },
	ctx: SearchCtx,
): Promise<ChunkResult>;

export function formatMatches(matches: SearchMatch[]): string;
export function formatReferences(symbol: string, rows: RefRow[]): string;
export function formatReadChunk(chunk: ChunkResult): string;
