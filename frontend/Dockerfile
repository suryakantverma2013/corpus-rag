# syntax=docker/dockerfile:1.7
#
# Corpus web image (T-605): the built SPA plus the edge proxy that makes the product
# single-origin. Build context is the REPOSITORY ROOT (see /.dockerignore).
#
#   docker build -f frontend/Dockerfile -t corpus-web:local .

# ---------------------------------------------------------------- build
FROM node:24-bookworm-slim AS build

WORKDIR /build

# .npmrc carries `legacy-peer-deps=true` and MUST be copied before `npm ci`: openapi-
# typescript still declares peer typescript@^5 while this project is on TypeScript 6, and
# without the flag the install fails outright rather than warning.
COPY frontend/package.json frontend/package-lock.json frontend/.npmrc ./
RUN npm ci

# `npm run build` is `api:generate && tsc -b && vite build`, and api:generate reads
# ../backend/openapi.json relative to the CWD — hence /backend/openapi.json beside /build.
#
# Running the real build rather than skipping to `tsc -b && vite build` is a deliberate
# choice: it regenerates src/api/schema.d.ts from the committed OpenAPI document and then
# compiles against it, so a backend route that changed without the client being regenerated
# FAILS THE IMAGE BUILD. With no CI in this repository, this is the only automated place
# that check can live.
COPY backend/openapi.json /backend/openapi.json
COPY frontend/ ./

RUN npm run build

# ---------------------------------------------------------------- runtime
# nginx-unprivileged runs as uid 101 and listens on 8080 (not 80) — which is why compose
# publishes ${WEB_PORT}:8080.
FROM nginxinc/nginx-unprivileged:1.29-alpine AS runtime

# Baked in, not bind-mounted, so the image is self-contained and a deployment cannot drift
# from the configuration it was tested with. Mount over the top while iterating locally.
COPY deployment/nginx/proxy.inc     /etc/nginx/conf.d/proxy.inc
COPY deployment/nginx/streaming.inc /etc/nginx/conf.d/streaming.inc
COPY deployment/nginx/default.conf  /etc/nginx/conf.d/default.conf

COPY --from=build /build/dist /usr/share/nginx/html

EXPOSE 8080
