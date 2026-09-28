import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig({ plugins: [react()], server: { port: 18201, strictPort: true, proxy: { '/api': { target: 'http://127.0.0.1:18200', changeOrigin: false } } } });
