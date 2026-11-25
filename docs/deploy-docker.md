# Running in Docker

One container runs the Rust server (`wed-server`): the scan engine, the API and the built
dashboard. Use it on a Linux server; on a desktop PC the [Windows app](desktop.md) is simpler.

## Build

```bash
docker build -t wednesday .
```

The first build compiles the Rust workspace and takes several minutes.

## Run

```bash
docker run -d --name wednesday --restart unless-stopped \
  -p 127.0.0.1:8765:8000 \
  -v wednesday-data:/data \
  wednesday
```

- Inside the container it listens on `0.0.0.0`, so the login is on. The first start
  logs a setup code: `docker logs wednesday`, then open the dashboard, choose *Create
  account* and enter it. Or create the account from the shell:
  `docker exec -it wednesday wednesday-rs --create-user NAME` (it also resets a
  password). See [Access](dashboard.md#access).
- `/data` is the data folder (`WEDNESDAY_HOME`): the database, settings, logs and its
  `.env`. Keep it on a volume so it survives a rebuild.
- Arguments after the image name go to `wednesday-rs`, for example
  `--source synthetic` for a preview without market data. Without it the server uses
  the source saved in Settings, TradingView on a fresh data folder.

## Reaching it from elsewhere

Put a reverse proxy with TLS in front, so the password and the session cookie never
travel in clear, for example nginx:

```nginx
# Server-sent events: no buffering, long reads.
location = /api/stream {
    proxy_pass http://127.0.0.1:8765;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_buffering off;
    proxy_read_timeout 1h;
}
location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
}
```

Keep the `Host` header: the server refuses a write whose `Origin` doesn't match the
host the page came from. Pass `X-Forwarded-Proto` (`proxy_set_header X-Forwarded-Proto
$scheme;`) so the session cookie is marked Secure, and `X-Forwarded-For` so the
login's rate limit counts each client rather than the proxy.

## Update

```bash
git pull
docker build -t wednesday .
docker rm -f wednesday
# then the same docker run command as above
```
