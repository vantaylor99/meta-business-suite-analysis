export interface TicketSummary {
	filename: string;
	stage: string;
	sequence: number | null;
	slug: string;
	description: string;
	prereq?: string;
	files?: string[];
}

export interface TicketDetail extends TicketSummary {
	body: string;
	raw: string;
}

export interface PipelineCounts {
	backlog: number;
	fix: number;
	plan: number;
	implement: number;
	review: number;
	blocked: number;
	complete: number;
}

export interface SiblingInfo {
	name: string;
	url: string;
}

export interface SearchMatch {
	path: string;
	start_line: number;
	end_line: number;
	text: string;
	score: number;
}

export interface SearchResults {
	query: string;
	k: number;
	pathFilter: string | null;
	matches: SearchMatch[];
}

export interface RefMatch {
	path: string;
	start_line: number;
	end_line: number;
	text: string;
}

export interface RefResults {
	symbol: string;
	max: number;
	pathFilter: string | null;
	rows: RefMatch[];
}

export interface ChunkResult {
	path: string;
	start: number;
	end: number;
	text: string;
}

export interface IndexStatus {
	exists: boolean;
	dbPath: string;
	files?: number;
	chunks?: number;
	dim?: number;
	modelId?: string;
	schemaVersion?: string;
	dbSizeBytes?: number;
	dbModifiedMs?: number;
}

export interface IndexConfig {
	source: string | null;
	defaults: { exclude: string[]; extensions: string[] };
	project: { exclude: string[]; include: string[]; extensions: string[] };
	effective: { exclude: string[]; include: string[]; extensions: string[] };
}

export interface IndexJob {
	id: string;
	kind: 'refresh' | 'rebuild';
	status: 'running' | 'success' | 'error';
	startedAt: number;
	endedAt?: number;
	exitCode?: number;
	logTail: string[];
}

export interface ApiError {
	error: string;
	message?: string;
}
