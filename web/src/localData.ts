/**
 * The 24-hour preview (a guest without an account, docs/design-guest-accounts.md): the routes that
 * keep a trader's own data (drawings, drawing templates, workspaces, the accepted terms) answer from
 * this browser's localStorage instead of the server. At login, `takeLocal` hands them over for
 * `POST /api/me/import` and clears them. Logged in, the dashboard's preferences (`wed.` / `xau.`
 * keys) follow the account through `/api/prefs` (`prefsDiff`).
 */

/** The keys this store uses: not `wed.`, so they are never taken for preferences or a layout. */
export const LOCAL_KEYS = {
  drawings: "wedlocal.drawings", // {symbol or "": {id: drawing}}
  templates: "wedlocal.templates", // {family: {name: template}}
  workspaces: "wedlocal.workspaces", // {name: workspace}
  terms: "wedlocal.terms", // {version, accepted_at}
} as const;

/** The storage this module reads and writes (localStorage in the browser, a map in tests). */
export interface KeyStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
  readonly length: number;
  key(index: number): string | null;
}

let preview = false;

/** Whether the dashboard runs as a preview: set by LoginGate. */
export function setPreview(on: boolean): void {
  preview = on;
}
export const inPreview = () => preview;

function store(): KeyStore | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

type Obj = Record<string, unknown>;

function read(s: KeyStore, key: string): Obj {
  try {
    const v = JSON.parse(s.getItem(key) ?? "{}");
    return v && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : {};
  } catch {
    return {};
  }
}

function write(s: KeyStore, key: string, value: Obj): void {
  try {
    s.setItem(key, JSON.stringify(value));
  } catch {
    /* storage full or blocked: the change isn't kept */
  }
}

const now = () => new Date().toISOString().replace(/\.\d+Z$/, "Z");
const squeeze = (name: string) => name.trim().replace(/\s+/g, " ");
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const refuse = (status: number, detail: string) => json(status, { detail });

function bodyOf(init?: RequestInit): Obj {
  if (typeof init?.body !== "string") return {};
  try {
    const v = JSON.parse(init.body);
    return v && typeof v === "object" ? (v as Obj) : {};
  } catch {
    return {};
  }
}

/**
 * The answer from this browser for a preview's call, or null when the server answers it.
 * `server` fetches from the server (the terms' text comes from there).
 */
export async function localAnswer(url: string, init: RequestInit | undefined, s: KeyStore | null = store(),
                                  server: (url: string) => Promise<Response> = (u) => fetch(u)): Promise<Response | null> {
  if (!s) return null;
  const method = (init?.method ?? "GET").toUpperCase();
  const u = new URL(url, "http://local");
  const path = u.pathname;
  const parts = path.split("/").slice(2).map(decodeURIComponent); // after "/api/"

  if (path === "/api/drawings/alerts" && method === "GET") return json(200, { alerts: [] });
  if (parts[0] === "drawings" && !(parts.length === 3 && parts[2] === "alert")) {
    const symbol = (u.searchParams.get("symbol") ?? "").trim().toUpperCase();
    const all = read(s, LOCAL_KEYS.drawings);
    const mine = (all[symbol] ?? {}) as Obj;
    if (parts.length === 1 && method === "GET") {
      return json(200, { source: "preview", symbol, drawings: Object.values(mine), alerts: [] });
    }
    if (parts.length === 2 && method === "PUT") {
      const drawing = { ...bodyOf(init), id: parts[1] };
      write(s, LOCAL_KEYS.drawings, { ...all, [symbol]: { ...mine, [parts[1]]: drawing } });
      return json(200, drawing);
    }
    if (parts.length === 2 && method === "DELETE") {
      if (!(parts[1] in mine)) return refuse(404, "No such drawing");
      const rest = { ...mine };
      delete rest[parts[1]];
      write(s, LOCAL_KEYS.drawings, { ...all, [symbol]: rest });
      return json(200, { deleted: parts[1] });
    }
    return null;
  }

  if (parts[0] === "drawing-templates") {
    const all = read(s, LOCAL_KEYS.templates);
    if (parts.length === 1 && method === "GET") {
      const templates = Object.entries(all).flatMap(([family, named]) =>
        Object.entries(named as Obj).map(([name, t]) => ({ family, name, ...(t as Obj) })));
      templates.sort((a, b) => a.family.localeCompare(b.family) || a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
      return json(200, { templates });
    }
    if (parts.length === 3) {
      const [, family, raw] = parts;
      const name = squeeze(raw);
      const named = { ...((all[family] ?? {}) as Obj) };
      if (method === "PUT") {
        if (!name) return refuse(422, "Give the template a name, e.g. H1 OB");
        const b = bodyOf(init);
        const t = { style: b.style ?? {}, text: b.text ?? null, saved_at: now() };
        write(s, LOCAL_KEYS.templates, { ...all, [family]: { ...named, [name]: t } });
        return json(200, { family, name, ...t });
      }
      if (method === "DELETE") {
        if (!(name in named)) return refuse(404, "No such template");
        delete named[name];
        write(s, LOCAL_KEYS.templates, { ...all, [family]: named });
        return json(200, { deleted: name });
      }
    }
    return null;
  }

  if (parts[0] === "workspaces") {
    const all = read(s, LOCAL_KEYS.workspaces);
    if (parts.length === 1 && method === "GET") {
      const workspaces = Object.entries(all)
        .map(([name, w]) => ({ name, saved_at: (w as Obj).saved_at, windows: ((w as Obj).windows as unknown[] | undefined)?.length ?? 0 }))
        .sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
      return json(200, { workspaces });
    }
    if (parts.length === 2) {
      const name = squeeze(parts[1]);
      if (method === "GET") {
        return name in all ? json(200, { name, ...(all[name] as Obj) }) : refuse(404, "No such workspace");
      }
      if (method === "PUT") {
        if (!name) return refuse(422, "Give the workspace a name, e.g. London prep");
        const b = bodyOf(init);
        const w = { layout: b.layout ?? {}, windows: b.windows ?? [], saved_at: now() };
        write(s, LOCAL_KEYS.workspaces, { ...all, [name]: w });
        return json(200, { name, ...w });
      }
      if (method === "DELETE") {
        const deleted = name in all;
        const rest = { ...all };
        delete rest[name];
        write(s, LOCAL_KEYS.workspaces, rest);
        return json(200, { deleted });
      }
    }
    return null;
  }

  if (path === "/api/terms" || path === "/api/terms/accept") {
    const res = await server("/api/terms");
    if (!res.ok) return res;
    const terms = (await res.json()) as Obj;
    let mine = read(s, LOCAL_KEYS.terms);
    if (method === "POST") {
      mine = { version: terms.version, accepted_at: now() };
      write(s, LOCAL_KEYS.terms, mine);
    }
    const accepted = mine.version === terms.version;
    return json(200, { ...terms, accepted, accepted_at: accepted ? mine.accepted_at ?? null : null });
  }
  return null;
}

/** What the preview kept, for `POST /api/me/import`. */
export interface LocalData {
  drawings: { symbol?: string; drawing: Obj }[];
  templates: Obj[];
  workspaces: Obj[];
  prefs: Obj;
}

export function localData(s: KeyStore | null = store()): LocalData {
  const out: LocalData = { drawings: [], templates: [], workspaces: [], prefs: {} };
  if (!s) return out;
  for (const [symbol, named] of Object.entries(read(s, LOCAL_KEYS.drawings))) {
    for (const d of Object.values(named as Obj)) out.drawings.push(symbol ? { symbol, drawing: d as Obj } : { drawing: d as Obj });
  }
  for (const [family, named] of Object.entries(read(s, LOCAL_KEYS.templates))) {
    for (const [name, t] of Object.entries(named as Obj)) out.templates.push({ family, name, ...(t as Obj) });
  }
  for (const [name, w] of Object.entries(read(s, LOCAL_KEYS.workspaces))) out.workspaces.push({ name, ...(w as Obj) });
  out.prefs = prefsOf(s);
  return out;
}

/** Whether the preview kept anything worth importing (drawings, templates or workspaces). */
export const hasLocalData = (d: LocalData) => d.drawings.length + d.templates.length + d.workspaces.length > 0;

/** Forget the preview's drawings, templates, workspaces and terms (they are the account's now). */
export function clearLocal(s: KeyStore | null = store()): void {
  if (!s) return;
  for (const k of Object.values(LOCAL_KEYS)) {
    try {
      s.removeItem(k);
    } catch {
      /* nothing to clear */
    }
  }
}

// ---- preferences that follow the account ----

export const isPrefKey = (k: string) => k.startsWith("wed.") || k.startsWith("xau.");

/** This browser's preferences, parsed (a value that isn't JSON is kept as its text). */
export function prefsOf(s: KeyStore | null = store()): Obj {
  const out: Obj = {};
  if (!s) return out;
  try {
    for (let i = 0; i < s.length; i++) {
      const k = s.key(i);
      if (!k || !isPrefKey(k)) continue;
      const raw = s.getItem(k);
      if (raw === null) continue;
      try {
        out[k] = JSON.parse(raw);
      } catch {
        out[k] = raw;
      }
    }
  } catch {
    /* storage unavailable */
  }
  return out;
}

/** Put the account's preferences in this browser. */
export function applyPrefs(prefs: Obj, s: KeyStore | null = store()): void {
  if (!s) return;
  for (const [k, v] of Object.entries(prefs)) {
    if (!isPrefKey(k)) continue;
    try {
      s.setItem(k, JSON.stringify(v));
    } catch {
      /* storage full: that one isn't applied */
    }
  }
}

/** What changed between two snapshots of the preferences, as `PUT /api/prefs` takes it (null removes). */
export function prefsDiff(before: Obj, after: Obj): Obj | null {
  const diff: Obj = {};
  for (const [k, v] of Object.entries(after)) if (JSON.stringify(before[k]) !== JSON.stringify(v)) diff[k] = v;
  for (const k of Object.keys(before)) if (!(k in after)) diff[k] = null;
  return Object.keys(diff).length ? diff : null;
}
