import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';

const configuredBasePath = process.env.BASE_PATH?.trim();
const base = configuredBasePath ? `/${configuredBasePath.replace(/^\/+|\/+$/g, '')}/` : '/';

export default defineConfig({
  base,
  plugins: [react()],
  server: {
    allowedHosts: ['d4f65eac244a12.lhr.life', '10.103.16.12'],
    proxy: { '/api': { target: 'http://127.0.0.1:8000', changeOrigin: false } },
  },
  build: {
    sourcemap: false,
    rollupOptions: {input: {
      workbench: fileURLToPath(new URL('./index.html', import.meta.url)),
      administration: fileURLToPath(new URL('./admin/index.html', import.meta.url)),
    }},
  },
});
