/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Backend origin, e.g. http://127.0.0.1:8000. Empty means same origin via the dev proxy. */
  readonly VITE_API_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
