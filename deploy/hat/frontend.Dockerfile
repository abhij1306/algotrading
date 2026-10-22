# Optional HAT frontend (compose profile "frontend"): the built app served by `vp preview`,
# which proxies /api to BACKEND_URL. Build context: the repository root.
FROM node:26-slim

WORKDIR /app/frontend
RUN corepack enable
COPY frontend/ ./
RUN pnpm install --frozen-lockfile && pnpm build

USER node
EXPOSE 3000
CMD ["pnpm", "preview", "--host", "0.0.0.0", "--port", "3000"]
