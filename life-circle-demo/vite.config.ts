import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
export default defineConfig({
  plugins: [react()],
  test: { include: ['src/**/*.test.ts'] },
  server: { port: 5173, strictPort: true },
  build: { chunkSizeWarningLimit: 1300 }
});
