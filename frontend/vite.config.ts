import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Relative asset URLs so the same build works at any mount prefix
// (Django `redline/`, FastAPI `/_redline`).
export default defineConfig({
  plugins: [react()],
  base: './',
  build: {
    // Native module imports keep offline snapshots independent of URL-based preload helpers.
    modulePreload: false,
    outDir: '../packages/redline-core/src/redline_core/ui/static',
    emptyOutDir: true,
  },
})
