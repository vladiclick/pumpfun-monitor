import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Backend-ul FastAPI ruleaza pe 8000. In dev, Vite face proxy ca sa nu avem CORS
// si ca frontend-ul sa foloseasca aceleasi cai relative ca in productie.
const BACKEND = process.env.BACKEND_URL || 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // 0.0.0.0 ca sa poti deschide dashboardul si de pe telefon, din aceeasi retea.
    host: true,
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true },
      '/ws': { target: BACKEND, ws: true, changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
