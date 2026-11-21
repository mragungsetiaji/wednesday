# Plugins

Features can live outside this repository, in a separate program Wednesday runs:
a **plugin**. Wednesday runs exactly the same without any. **Settings > Plugins**
lists what's installed and why a plugin isn't running. `GET /api/plugins` gives the
same list plus the feature ids the running plugins provide, which the dashboard uses
to unlock screens.

This is plugin API 2 (#96, step 10). API 1, Python packages loaded into the Python
server through the `wednesday.plugins` entry point group, is gone from the app with
the Python server: a folder left from an API 1 download is listed with "written for
plugin API 1, this Wednesday has 2" and not run. Entering the licence key again
downloads the API 2 build.

## Getting Wednesday EE

Users don't install anything by hand: they enter their licence key in
**Settings > Plan**. When no plugin handles licences and the key comes from the
licence server (`WEDK-...`), Wednesday (`server-rs/src/plugins/install.rs`):

1. sends the key, this install's device id and the target it was built for (a Rust
   target triple such as `x86_64-pc-windows-msvc`) to `POST /v1/download` on the
   licence server (`WEDNESDAY_LICENCE_SERVER`);
2. checks the answer: a manifest (name, version, plugin API, target, file name,
   SHA-256) signed with Ed25519 by a key in `server-rs/src/plugins/keys.rs`, and a
   zip whose hash matches it. Only `wednesday_ee`, for API 2 and this build's target,
   no larger than 50 MB, is accepted; nothing downloaded runs before this check;
3. unpacks the zip to `data/plugins/wednesday_ee.new/` (`WEDNESDAY_PLUGINS_DIR`),
   refusing entries that would land outside it (`..`, absolute paths, symlinks), stops
   an older copy and puts the new one in its place whole;
4. starts it straight away, without a restart, and hands it the key to activate.

The public keys in `keys.rs` are a copy of `src/wednesday/plugin_keys.py` (a test
checks they match); they are safe to publish, the matching private key stays with
whoever signs. A free user never gets Wednesday EE's code: the licence server hands
it out only for a usable key. The desktop app keeps `data/` in its home folder
(`%LOCALAPPDATA%\Wednesday`).

Free, with or without a licence: the screener, alerts, the bias, the news card and
one journal.

## What a plugin is

A folder in `data/plugins/` holding a program and its `plugin.json`:

```json
{"name": "wednesday_ee", "version": "0.2.0", "api": 2, "exe": "wednesday-ee"}
```

(`"wednesday-ee.exe"` on Windows.) The name is lowercase letters and `_`. The
download is a zip of that folder; the licence server signs it as above.

## How Wednesday runs it

At startup (once the server listens) and after a download, the server
(`server-rs/src/plugins/mod.rs`) reads each `data/plugins/*/plugin.json` and starts
the program with its folder as the working directory, standard input and output
piped, standard error into Wednesday's log, and:

| Variable | What it is |
| --- | --- |
| `WEDNESDAY_PLUGIN_TOKEN` | Random, new each launch. The plugin answers only requests with `authorization: Bearer <token>` (401 otherwise) |
| `WEDNESDAY_CORE_URL` | `http://127.0.0.1:<port>` of the server, for calls back |
| `WEDNESDAY_CORE_TOKEN` | Another random token, for those calls |
| `WEDNESDAY_DEVICE_ID` | This install's id (setting `ee_device`), which licences are bound to |
| `WEDNESDAY_DATA_DIR` | `data/plugins/<name>/state/`, the plugin's own folder for files |
| `WEDNESDAY_LICENCE_SERVER` | Passed on when set |

The Telegram bot token (and `WEDNESDAY_EE_TEST_KEYS`) is not passed on.

The plugin listens on `127.0.0.1:0` and prints one line on standard output within
10 seconds:

```
WEDNESDAY-PLUGIN-LISTEN <port>
```

otherwise it is stopped and listed with "didn't start". It must exit when its
standard input closes: that is how it follows Wednesday when Wednesday exits or
crashes. Wednesday also stops it on shutdown. A plugin that stops is listed with its
exit status and restarted up to 3 times, waiting longer each time.

## What a plugin answers (all with the bearer token)

- `GET /plugin` → `{"name", "version", "api": 2, "features": ["journal.multi", ...]}`.
  Read after start, after every licence call, and every 60 seconds, so features can
  follow the licence.
- The licence, for one plugin at most (`404` means it doesn't handle licences):
  `GET /licence` → the status object; `PUT /licence {"key"}` → the status, or
  `422 {"detail": "<message for the user>"}`; `DELETE /licence` → the status.
  Wednesday serves them as `GET / PUT / DELETE /api/licence` (`{"available": true,
  ...status}`) for **Settings > Plan**.
- `ANY /routes/<path>`: the dashboard's `/api/ee/<short name>/<path>` (the name
  without `wednesday_`: Wednesday EE's routes stay at `/api/ee/ee/...`), forwarded with its
  method, query, body and content type, the answer streamed back. Cross-site writes
  are refused before they reach the plugin.

There are no scan hooks: a plugin that needs the scan reads the server's public API
(`GET <core>/api/scan`, ...) like the dashboard does.

## What Wednesday answers a plugin

`GET / PUT / DELETE <core>/api/plugin-host/settings/<key>` with
`authorization: Bearer <WEDNESDAY_CORE_TOKEN>`: the server's settings table, only
keys that start with the plugin's name without `wednesday_` and an underscore
(Wednesday EE: `ee_...`), 403 otherwise, 401 without the token. `GET` →
`{"value": <JSON or null>}`; `PUT` takes the JSON value as its body and answers
`{"ok": true}`; so does `DELETE`. Values are never logged. These routes are for
plugins only and are not part of the dashboard's API contract.

## Features

`server-rs/src/plugins/catalog.rs` (a copy of `wednesday/features.py`) lists the paid
feature ids with a title and a line on what each does, so the Plan screen can show
them, locked, even when no plugin is installed. A plugin can provide ids that aren't
in the list; they show too. The core's own checks (a second journal needs
`journal.multi`) read the running plugins' features; licence checks themselves stay
in the plugin.

## Building one

Any language works; Wednesday EE is Rust (the private `wednesday-ee` repository).
A plugin needs: an HTTP server on `127.0.0.1:0` that checks the bearer token on every
request, the handshake line, `GET /plugin`, exiting when standard input closes, and
its program built for each target Wednesday ships for. `server-rs/examples/fake_plugin.rs`
is a small complete one the tests run. To try a build by hand, put the folder in
`data/plugins/<name>/` (or point `WEDNESDAY_PLUGINS_DIR` elsewhere) and restart
Wednesday.

## What a plugin can and can't protect

Code in a plugin is installed on the user's machine, where it can be read.
Keeping it in a private repository, and handing it out only for a licence key,
keeps it away from free users, not from whoever has a licence. Anything that must be enforced (licences for
hosted services, downloads) belongs on a server the plugin talks to.
