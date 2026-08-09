import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In development Vite serves the UI and proxies /api to uvicorn. In production
// there is no proxy at all: FastAPI serves the built dist/ and the API from one
// origin, which is what makes the deployment a single Tailscale Funnel target.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
