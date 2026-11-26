# Design: a 24-hour preview, sign-up with approval, and per-user data

Status: implemented. Follows [design-storage-users.md](design-storage-users.md), which added
accounts and the login for a network host.

## What changes for whoever opens the dashboard

On a network host (the login is on), someone without a session now gets a **24-hour preview**
instead of the login screen:

- The chart, the scan, the levels, the markets and the news calendar all work.
- Drawings, drawing templates, workspaces and layout preferences are kept **in this browser**
  (localStorage), as nothing is tied to an account yet.
- A banner says how long the preview lasts, with *Log in* and *Sign up*.
- After 24 hours the preview ends and the dashboard shows the **login wall** (log in, or sign up).

**Sign up** asks for a username and a password (and an optional email). The account is
**pending**: it can't log in until the admin approves it in **Settings > Users**, where the
admin can also reject it, disable an account, or make it an admin.

**Logging in** (from the preview or the wall) moves what the browser kept into the account: its
drawings, templates, workspaces and preferences go to the database, and the browser's copies of
the drawings, templates and workspaces are cleared. From then on everything is saved per user,
so it follows the account to another PC or laptop.

On `127.0.0.1` (the desktop app, `make serve`) nothing changes: there is no login, no preview and
no users, and everything is saved as before.

## Who can do what

| | Preview (guest) | User | Admin (`owner`) |
| --- | --- | --- | --- |
| Chart, scan, levels, markets, calendar, replay (read) | yes | yes | yes |
| Drawings, templates, workspaces, preferences | in the browser | own, in the database | own |
| Journal | no (log in) | own journals | own journals |
| Drawing price alerts (sent by the Telegram bot) | no | no | yes |
| Bias (it drives the scan and the alerts) | read | read | set |
| Data source, Markets, Telegram, news import, licence, plugins | no | read only | change |
| Settings > Users | no | no | yes |

Drawing alerts and the bias stay with the admin because the Telegram bot and the scan are one per
server: a user's alert would go to the admin's chat.

## The preview pass

`POST /api/auth/guest` gives a pass: a random token in an HttpOnly cookie `wednesday_guest`
(Max-Age 24 h, SameSite=Strict, Secure behind HTTPS). The `guests` table keeps only its SHA-256,
the address it was given to and when it ends.

- One preview per address per 7 days: asked again while it runs, the same pass comes back with
  the same end; once it has ended, `POST /api/auth/guest` answers 403 `The 24-hour preview ended.
  Log in, or sign up for an account.` Clearing the cookies doesn't start a new preview. (Several
  people behind one address share it.)
- A pass reads; with it, a guest can only call `GET` routes on the guest list (market data,
  calendar, bias, terms, settings, plugins, licence, drawings, workspaces, templates, prefs,
  journals). Anything else answers 403 `Log in or sign up for this`; without a pass or a session,
  401 `Log in first`.
- A user calling an admin route gets 403 `Only the admin can change this`.
- The pass isn't an account: nothing is written to the database for a guest except the pass row.

`GET /api/auth/status` adds `guest` (`{"until": ISO}` or null), `guest_available` (a pass can be
had from this address) and `signup` (sign-up is open; always true with the login on).

## Accounts

`users.role` is `owner` (admin), `user`, or `pending` (signed up, not yet approved). No new
columns: the existing `disabled` turns an account off.

- `POST /api/auth/signup` `{username, password, email?}` makes a `pending` account. It is rate
  limited per address like the login, and answers without a session.
- A pending account's login answers 403 `Your account is waiting for the admin's approval`.
- `GET /api/users` (admin) lists the accounts. `PATCH /api/users/{id}` `{role, disabled}`
  approves (`pending` to `user`), promotes, demotes or disables. `DELETE /api/users/{id}` rejects
  or removes an account with its data. The last admin can't be demoted, disabled or removed.
- The first account (setup code, `--create-user`, `XAU_AUTH_PASSWORD`) is an `owner`, as before;
  `--create-user` on a new name makes an `owner` too.

## Per-user data

| Data | Where | Scoped by |
| --- | --- | --- |
| Drawings | `drawings` | new column `user_id` |
| Drawing alerts | `drawing_alerts` | the drawing's `user_id` |
| Journals, with trades, cash, notes and entries | `journals` | new column `user_id`; every `/api/journals/{id}/...` route checks it |
| Workspaces, drawing templates | `settings` | key `workspaces:<user id>`, `drawing_templates:<user id>` |
| Preferences (layout, chart type, ...) | `settings` | key `prefs:<user id>` |
| Accepted terms | `settings` | key `terms:<user id>` |

`user_id` null is the data of a server without a login. When the login is on, the server gives
every null row and unkeyed setting to the oldest admin at start, so the trader's existing
drawings and journals become theirs.

New routes:

- `GET /api/prefs`, `PUT /api/prefs` `{key: value | null}` (merge; null removes).
- `POST /api/me/import` `{drawings: [{source, symbol, drawing}], templates, workspaces, prefs}`:
  what the browser kept during the preview, merged into the account (a drawing id that exists
  already is skipped).

## The dashboard

- `LoginGate`: checking, then open (no login, or logged in), preview (a pass), or the wall
  (login, sign up, or create the first account with the setup code).
- In the preview, the drawings, templates and workspaces API calls go to a local store
  (`localData.ts`) instead of the server; the preview banner sits above the app.
- After a login: import the browser's data, clear it, load the account's preferences into
  localStorage, then mount the app. While logged in, a changed preference is also saved to the
  account (debounced).
- Settings: **Users** (admin) with the pending accounts first; the admin-only sections are hidden
  for a user, and Data source shows read only.

## Build order

1. Store: `guests` table, `user_id` on `drawings` and `journals`, per-user settings keys, the
   claim at start (Rust and Python).
2. Auth: roles, sign-up, the guest pass, the route rules (guest list, admin list), Settings > Users
   routes (Rust and Python, contract, tests).
3. Per-user routes: drawings, alerts, journals, workspaces, templates, terms, prefs, import.
4. Dashboard: LoginGate with the preview and the wall, local store, import on login, prefs sync,
   Settings > Users and the admin-only sections.
5. Docs: dashboard.md (Access), deploy-docker.md, AGENTS.md.
