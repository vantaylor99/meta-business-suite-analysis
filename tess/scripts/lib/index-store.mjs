/**
 * Vector index storage on top of sqlite + sqlite-vec.
 *
 * One file per project: tickets/.index/index.db
 *
 *   files       (path PK, content_hash, mtime_ms, chunk_count)
 *   chunks      (id PK, path FK, start_line, end_line, text)
 *   chunk_vec   vec0 virtual table — embedding FLOAT[dim]
 *   meta        (key PK, value)
 *
 * The vec0 row id is the chunks.id, so a single delete on chunks cascades
 * the vec row through a trigger.  Embedding dim and model id are recorded
 * in meta so a model swap is detected as a hard rebuild.
 */

import { mkdir } from 'node:fs/promises';
import { dirname } from 'node:path';
import Database from 'better-sqlite3';
import * as sqliteVec from 'sqlite-vec';

export const SCHEMA_VERSION = '1';

export class IndexStore {
	constructor(db, dim) {
		this.db = db;
		this.dim = dim;
	}

	static async open(dbPath, { dim, modelId, readonly = false } = {}) {
		await mkdir(dirname(dbPath), { recursive: true });
		const db = new Database(dbPath, { readonly });
		sqliteVec.load(db);
		db.pragma('journal_mode = WAL');
		db.pragma('synchronous = NORMAL');

		if (!readonly) {
			ensureSchema(db, dim);
			const existingDim = readMeta(db, 'embedding_dim');
			const existingModel = readMeta(db, 'model_id');
			if (existingDim && Number(existingDim) !== dim) {
				db.close();
				throw new Error(
					`Index dim ${existingDim} != configured ${dim}. Run with --rebuild to replace.`,
				);
			}
			if (existingModel && modelId && existingModel !== modelId) {
				db.close();
				throw new Error(
					`Index model "${existingModel}" != configured "${modelId}". Run with --rebuild to replace.`,
				);
			}
			writeMeta(db, 'embedding_dim', String(dim));
			if (modelId) writeMeta(db, 'model_id', modelId);
			writeMeta(db, 'schema_version', SCHEMA_VERSION);
		} else {
			const existingDim = readMeta(db, 'embedding_dim');
			if (!existingDim) {
				db.close();
				throw new Error('Index DB has no embedding_dim — run the indexer first.');
			}
			dim = Number(existingDim);
		}

		return new IndexStore(db, dim);
	}

	close() { this.db.close(); }

	getMeta(key) { return readMeta(this.db, key); }
	setMeta(key, value) { writeMeta(this.db, key, value); }

	getFile(path) {
		return this.db.prepare('SELECT path, content_hash, mtime_ms, chunk_count FROM files WHERE path = ?').get(path);
	}

	listFilePaths() {
		return this.db.prepare('SELECT path FROM files').all().map(r => r.path);
	}

	stats() {
		const files = this.db.prepare('SELECT COUNT(*) AS n FROM files').get().n;
		const chunks = this.db.prepare('SELECT COUNT(*) AS n FROM chunks').get().n;
		return { files, chunks };
	}

	/**
	 * Replace all chunks for a file. Called after re-embedding.
	 * `rows` is [{ start_line, end_line, text, embedding (Float32Array) }, ...]
	 */
	replaceFile(path, contentHash, mtimeMs, rows) {
		const tx = this.db.transaction(() => {
			this.deleteFile(path);
			const insertFile = this.db.prepare(
				'INSERT INTO files (path, content_hash, mtime_ms, chunk_count) VALUES (?, ?, ?, ?)',
			);
			insertFile.run(path, contentHash, mtimeMs, rows.length);

			const insertChunk = this.db.prepare(
				'INSERT INTO chunks (path, start_line, end_line, text) VALUES (?, ?, ?, ?)',
			);
			const insertVec = this.db.prepare(
				'INSERT INTO chunk_vec (chunk_id, embedding) VALUES (?, ?)',
			);

			for (const row of rows) {
				const info = insertChunk.run(path, row.start_line, row.end_line, row.text);
				insertVec.run(BigInt(info.lastInsertRowid), vecBuffer(row.embedding));
			}
		});
		tx();
	}

	deleteFile(path) {
		const tx = this.db.transaction(() => {
			const ids = this.db.prepare('SELECT id FROM chunks WHERE path = ?').all(path).map(r => r.id);
			if (ids.length > 0) {
				const placeholders = ids.map(() => '?').join(',');
				this.db.prepare(`DELETE FROM chunk_vec WHERE chunk_id IN (${placeholders})`).run(...ids);
			}
			this.db.prepare('DELETE FROM chunks WHERE path = ?').run(path);
			this.db.prepare('DELETE FROM files WHERE path = ?').run(path);
		});
		tx();
	}

	/**
	 * Top-k semantic search. `pathFilter` is an optional SQL LIKE pattern (e.g. "src/%").
	 */
	knn(queryEmbedding, k = 10, pathFilter = null) {
		const sql = pathFilter
			? `
				SELECT c.path, c.start_line, c.end_line, c.text, v.distance
				FROM chunk_vec v
				JOIN chunks c ON c.id = v.chunk_id
				WHERE v.embedding MATCH ? AND k = ? AND c.path LIKE ?
				ORDER BY v.distance
			`
			: `
				SELECT c.path, c.start_line, c.end_line, c.text, v.distance
				FROM chunk_vec v
				JOIN chunks c ON c.id = v.chunk_id
				WHERE v.embedding MATCH ? AND k = ?
				ORDER BY v.distance
			`;
		const params = pathFilter
			? [vecBuffer(queryEmbedding), k, pathFilter]
			: [vecBuffer(queryEmbedding), k];
		const rows = this.db.prepare(sql).all(...params);
		return rows.map(r => ({
			path: r.path,
			start_line: r.start_line,
			end_line: r.end_line,
			text: r.text,
			score: 1 - r.distance,
		}));
	}

	/**
	 * Literal substring search.  `needle` may contain `|` to OR multiple
	 * alternatives (each side is still treated as a literal substring, NOT a
	 * regex) — e.g. "composeNewSlot|defaultComposeNewSlot" matches either.
	 */
	grepLiteral(needle, max = 50, pathFilter = null) {
		const terms = needle.split('|').map(t => t.trim()).filter(Boolean);
		if (terms.length === 0) return [];
		const escapeLike = s => `%${s.replace(/[\\%_]/g, ch => '\\' + ch)}%`;
		const orClause = terms.map(() => 'text LIKE ? ESCAPE \'\\\'').join(' OR ');
		const sql = pathFilter
			? `SELECT path, start_line, end_line, text FROM chunks WHERE (${orClause}) AND path LIKE ? ORDER BY path, start_line LIMIT ?`
			: `SELECT path, start_line, end_line, text FROM chunks WHERE (${orClause}) ORDER BY path, start_line LIMIT ?`;
		const params = pathFilter
			? [...terms.map(escapeLike), pathFilter, max]
			: [...terms.map(escapeLike), max];
		return this.db.prepare(sql).all(...params);
	}
}

function ensureSchema(db, dim) {
	db.exec(`
		CREATE TABLE IF NOT EXISTS files (
			path          TEXT PRIMARY KEY,
			content_hash  TEXT NOT NULL,
			mtime_ms      INTEGER NOT NULL,
			chunk_count   INTEGER NOT NULL
		);
		CREATE TABLE IF NOT EXISTS chunks (
			id            INTEGER PRIMARY KEY AUTOINCREMENT,
			path          TEXT NOT NULL,
			start_line    INTEGER NOT NULL,
			end_line      INTEGER NOT NULL,
			text          TEXT NOT NULL
		);
		CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
		CREATE TABLE IF NOT EXISTS meta (
			key           TEXT PRIMARY KEY,
			value         TEXT NOT NULL
		);
	`);
	db.exec(`
		CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vec USING vec0(
			chunk_id      INTEGER PRIMARY KEY,
			embedding     FLOAT[${dim}]
		);
	`);
}

function readMeta(db, key) {
	const row = db.prepare('SELECT value FROM meta WHERE key = ?').get(key);
	return row?.value ?? null;
}

function writeMeta(db, key, value) {
	db.prepare('INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value').run(key, value);
}

function vecBuffer(arr) {
	if (arr instanceof Float32Array) return Buffer.from(arr.buffer, arr.byteOffset, arr.byteLength);
	const f32 = new Float32Array(arr);
	return Buffer.from(f32.buffer);
}
