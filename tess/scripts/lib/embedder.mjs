/**
 * Local embedding via @huggingface/transformers (transformers.js).
 *
 * Default model: jinaai/jina-embeddings-v2-base-code (768-dim, ~155MB
 * quantized).  Trained on aligned code/text pairs across ~30 programming
 * languages, so it scores actual source against natural-language queries
 * far better than a general sentence-transformer.  First run downloads the
 * `model_quantized.onnx` variant to TRANSFORMERS_CACHE (we point this at
 * tickets/.index/models/ so all artifacts stay under the project).
 *
 * Usage:
 *   const embedder = await Embedder.load(modelDir);
 *   const vectors = await embedder.embed(['text1', 'text2']);   // Float32Array[]
 */

export const DEFAULT_MODEL = 'jinaai/jina-embeddings-v2-base-code';
export const DEFAULT_DIM = 768;
// Quantized 768-dim base model is ~10x slower than MiniLM per embedding on
// CPU but produces meaningfully sharper code↔query rankings.  Keep per-batch
// size low to bound peak memory: an unlucky batch of long-line chunks can
// otherwise OOM the ONNX runtime ("bad allocation" on the first attention
// layer).  Combined with token-level truncation below, this keeps a single
// forward pass at ~BATCH_SIZE * MAX_TOKENS tokens.
const BATCH_SIZE = 4;
const MAX_TOKENS = 512;

export class Embedder {
	constructor(pipeline, modelId) {
		this.pipeline = pipeline;
		this.modelId = modelId;
	}

	static async load(cacheDir, modelId = DEFAULT_MODEL) {
		// Point transformers.js at our local cache before importing it so the
		// env settings take effect on first load.
		process.env.TRANSFORMERS_CACHE = cacheDir;
		const transformers = await import('@huggingface/transformers');
		transformers.env.cacheDir = cacheDir;
		transformers.env.allowLocalModels = true;
		// Suppress the "ONNX Runtime: …" startup banner on stderr that some
		// MCP clients flag as noise.
		transformers.env.backends?.onnx?.wasm && (transformers.env.backends.onnx.wasm.proxy = false);

		// dtype: 'q8' selects the `_quantized.onnx` variant when present.
		// transformers.js v3 dropped the v2-era `quantized: true` flag.
		const pipeline = await transformers.pipeline('feature-extraction', modelId, {
			dtype: 'q8',
		});
		return new Embedder(pipeline, modelId);
	}

	async embedOne(text) {
		const out = await this.pipeline(text, {
			pooling: 'mean', normalize: true,
			truncation: true, max_length: MAX_TOKENS,
		});
		return new Float32Array(out.data);
	}

	/**
	 * Embed an array of strings; returns Float32Array[] in input order.
	 * Batched internally to amortize model overhead.  Fails loudly if the
	 * model returns an unexpected dim (caller passes the expected value so
	 * we can refuse to silently corrupt the index).
	 */
	async embed(texts, expectedDim = null) {
		const result = new Array(texts.length);
		for (let i = 0; i < texts.length; i += BATCH_SIZE) {
			const batch = texts.slice(i, i + BATCH_SIZE);
			const out = await this.pipeline(batch, {
				pooling: 'mean', normalize: true,
				truncation: true, max_length: MAX_TOKENS,
			});
			// transformers returns a single Tensor of shape [batch, dim]; slice it.
			const dim = out.dims[1];
			if (expectedDim !== null && dim !== expectedDim) {
				throw new Error(
					`embedder for ${this.modelId} returned dim ${dim}, expected ${expectedDim}`,
				);
			}
			const flat = out.data;
			for (let j = 0; j < batch.length; j++) {
				const v = new Float32Array(dim);
				for (let k = 0; k < dim; k++) v[k] = flat[j * dim + k];
				result[i + j] = v;
			}
		}
		return result;
	}
}
