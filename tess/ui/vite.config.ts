import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { resolve } from 'node:path';
import { tessApi } from './src/server/api-plugin.js';

const projectRoot = process.env.TESS_PROJECT_ROOT || resolve(process.cwd(), '../..');

export default defineConfig({
	plugins: [
		svelte(),
		tessApi({
			projectRoot,
			ticketsDir: resolve(projectRoot, 'tickets'),
			siblingDir: resolve(projectRoot, 'teamos'),
		}),
	],
	server: {
		port: 3004,
	},
});
