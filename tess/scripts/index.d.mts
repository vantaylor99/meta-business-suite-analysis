/**
 * Type declarations for index.mjs.
 *
 * Authoritative source is the .mjs; this file exists so TypeScript consumers
 * (notably tess/ui) can call the loader and read the default lists without
 * us migrating the runtime to .ts.
 */

export const ALWAYS_EXCLUDE: readonly string[];
export const DEFAULT_EXTS: ReadonlySet<string>;

export interface IndexConfig {
	source: string | null;
	exclude: string[];
	include: string[];
	extensions: string[];
}

export function loadIndexConfig(repoRoot: string): Promise<IndexConfig>;

export function makeShouldIndex(config: IndexConfig): (relPath: string) => boolean;

export interface IndexerResult {
	scanned: number;
	reindexed: number;
	pruned: number;
	chunks: number;
}

export interface IndexerOptions {
	repoRoot: string;
	dbPath: string;
	modelCacheDir: string;
	rebuild?: boolean;
	config?: IndexConfig;
	log?: (msg: string) => void;
}

export function runIndexer(opts: IndexerOptions): Promise<IndexerResult>;
