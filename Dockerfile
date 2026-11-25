# Wednesday's Rust server (wed-server: scan engine, API and the built dashboard) in one container.
# Build: docker build -t wednesday .   See docs/deploy-docker.md.

FROM node:22-bookworm-slim AS web
WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM rust:1-bookworm AS server
WORKDIR /app
COPY . .
RUN cargo build --release -p wednesday-rs \
    && cp target/release/wednesday-rs /usr/local/bin/wednesday-rs

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 wednesday \
    && mkdir -p /data && chown wednesday:wednesday /data
COPY --from=server /usr/local/bin/wednesday-rs /usr/local/bin/wednesday-rs
COPY --from=web /app/web/dist /app/web
USER wednesday
# /data holds the database, settings and logs; keep it on a volume.
ENV WEDNESDAY_HOME=/data XAU_UI_DIR=/app/web XAU_HOST=0.0.0.0 XAU_PORT=8000
VOLUME ["/data"]
EXPOSE 8000
# Listening on 0.0.0.0 turns the login on: the first start logs a setup code to create the
# account with (or set XAU_AUTH_PASSWORD, or run `wednesday-rs --create-user NAME`). Serve it over
# HTTPS (a reverse proxy) when it is reachable from outside, so the password isn't sent in clear.
ENTRYPOINT ["wednesday-rs"]
