/**
 * Language-naive chunking.
 *
 *   - Markdown: split on heading boundaries (#, ##, ...).  A heading and the
 *     prose below it stay together; if a section runs past the line target
 *     it is sliced into multiple chunks with overlap.
 *   - Code/everything else: pack lines into ~CHUNK_LINES windows that
 *     prefer to break on blank lines.  Adjacent windows share OVERLAP_LINES
 *     to keep cross-window references retrievable.
 *
 * Chunk shape: { start_line (1-based, inclusive), end_line, text }.
 * Empty/blank-only chunks are dropped.
 *
 * No tree-sitter — code-aware splitting is a future enhancement; the
 * line-window approach is good enough to start and avoids a large native
 * dependency surface on Windows.
 */

const CHUNK_LINES = 40;
const OVERLAP_LINES = 8;
const MAX_CHUNK_LINES = 80;

// Hard byte cap on chunk text — minified/generated/single-line files can
// produce 80-line chunks that are tens of KB and tokenize to thousands of
// tokens, which OOMs the embedder under batched inference.  Truncating here
// keeps line-range metadata accurate (still points at the original lines)
// while bounding the embed-content.  Roughly correlates with ~4-8KB ≈ 1-2K
// tokens at typical code density.
const MAX_CHUNK_BYTES = 6000;

export function chunkText(text, path) {
	const isMarkdown = /\.(md|mdx)$/i.test(path);
	const lines = text.split(/\r?\n/);
	const chunks = isMarkdown ? chunkMarkdown(lines) : chunkLines(lines);
	return chunks
		.filter(c => c.text.trim().length > 0)
		.map(capChunkBytes);
}

function capChunkBytes(chunk) {
	if (chunk.text.length <= MAX_CHUNK_BYTES) return chunk;
	return {
		...chunk,
		text: chunk.text.slice(0, MAX_CHUNK_BYTES) + '\n… (truncated)',
	};
}

function chunkLines(lines) {
	const chunks = [];
	let i = 0;
	while (i < lines.length) {
		const targetEnd = Math.min(lines.length, i + CHUNK_LINES);
		let end = targetEnd;
		// Prefer to break on a blank line within the next OVERLAP_LINES of the target.
		const searchUntil = Math.min(lines.length, targetEnd + OVERLAP_LINES);
		for (let j = targetEnd; j < searchUntil; j++) {
			if (lines[j] === '' || lines[j].trim() === '') { end = j; break; }
		}
		// Hard cap: never exceed MAX_CHUNK_LINES.
		if (end - i > MAX_CHUNK_LINES) end = i + MAX_CHUNK_LINES;
		const slice = lines.slice(i, end);
		chunks.push({
			start_line: i + 1,
			end_line: i + slice.length,
			text: slice.join('\n'),
		});
		if (end >= lines.length) break;
		// Step forward by CHUNK_LINES - OVERLAP_LINES so adjacent chunks overlap.
		const step = Math.max(1, CHUNK_LINES - OVERLAP_LINES);
		i += step;
	}
	return chunks;
}

function chunkMarkdown(lines) {
	const sections = [];
	let current = { start: 0, lines: [] };
	for (let i = 0; i < lines.length; i++) {
		const line = lines[i];
		if (/^#{1,6}\s/.test(line) && current.lines.length > 0) {
			sections.push(current);
			current = { start: i, lines: [] };
		}
		current.lines.push(line);
	}
	if (current.lines.length > 0) sections.push(current);

	const chunks = [];
	for (const section of sections) {
		if (section.lines.length <= MAX_CHUNK_LINES) {
			chunks.push({
				start_line: section.start + 1,
				end_line: section.start + section.lines.length,
				text: section.lines.join('\n'),
			});
		} else {
			// Long section: subchunk it with the line-window strategy, but keep the
			// section's heading as a prefix on each subchunk so retrieval still
			// surfaces the right context.
			const heading = section.lines[0];
			const sub = chunkLines(section.lines);
			for (const c of sub) {
				const start = section.start + c.start_line;
				const end = section.start + c.end_line;
				const text = c.start_line === 1 ? c.text : `${heading}\n…\n${c.text}`;
				chunks.push({ start_line: start, end_line: end, text });
			}
		}
	}
	return chunks;
}
