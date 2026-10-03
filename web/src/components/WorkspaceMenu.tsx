import { useEffect, useId, useState } from "react";

import { deleteWorkspace, fetchWorkspace, fetchWorkspaces, saveWorkspace, type WorkspaceWindow } from "../api";
import { useDesktopApi } from "../desktop";
import { applyLayout, currentLayout } from "../workspaces";
import { usePopover } from "./usePopover";

type Item = { name: string; saved_at: string; windows: number };

/**
 * Workspaces: save the chart layout (and, in the desktop app, every window with where it sits) under a
 * name, and open one later. Opening applies the layout, rearranges the windows and reloads the page.
 */
export function WorkspaceMenu() {
  const { open, setOpen, ref } = usePopover<HTMLDivElement>();
  const id = useId();
  const desktop = useDesktopApi();
  const [items, setItems] = useState<Item[] | null>(null);
  const [name, setName] = useState("");
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => fetchWorkspaces().then((r) => setItems(r.workspaces)).catch((e) => {
    setItems([]);
    setMessage({ kind: "error", text: e instanceof Error ? e.message : String(e) });
  });
  useEffect(() => {
    if (open) load();
  }, [open]);

  const run = async (fn: () => Promise<string | void>) => {
    setBusy(true);
    setMessage(null);
    try {
      const text = await fn();
      if (text) setMessage({ kind: "ok", text });
    } catch (e) {
      setMessage({ kind: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const save = (n: string) => run(async () => {
    const windows: WorkspaceWindow[] = desktop?.window_layout ? await desktop.window_layout() : [];
    await saveWorkspace(n, currentLayout(), windows);
    setName("");
    await load();
    return windows.length > 1 ? `Saved ${n} with ${windows.length} windows.` : `Saved ${n}.`;
  });

  const openWorkspace = (n: string) => run(async () => {
    const ws = await fetchWorkspace(n);
    applyLayout(ws.layout);
    if (desktop?.arrange) {
      await desktop.arrange(ws.windows);
    } else {
      for (const w of ws.windows.filter((x) => x.route !== "#")) {
        window.open(`${window.location.pathname}${w.route}`, `wed-${w.route}`,
          `popup,left=${w.x},top=${w.y},width=${w.width},height=${w.height}`);
      }
    }
    window.location.reload();
  });

  return (
    <div className="workspace-menu" ref={ref}>
      <button type="button" className="button quiet" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}
        title="Workspaces: save this layout and its windows, open a saved one">
        Workspace
      </button>
      {open && (
        <div className="chart-pop workspace-pop" id={id} role="group" aria-label="Workspaces">
          {items === null ? <p className="field-hint">Loading…</p> : items.length === 0 ? (
            <p className="field-hint">No workspaces yet. Arrange the charts{desktop ? " and windows" : ""}, then save them here.</p>
          ) : (
            <ul className="workspace-list">
              {items.map((w) => (
                <li key={w.name}>
                  <button type="button" className="link-button" disabled={busy} onClick={() => openWorkspace(w.name)}
                    title={`Open ${w.name}`}>
                    {w.name}
                  </button>
                  <span className="muted num">{w.windows > 1 ? `${w.windows} windows` : ""}</span>
                  <button type="button" className="link-button" disabled={busy} title={`Save the current layout as ${w.name}`}
                    onClick={() => window.confirm(`Replace ${w.name} with the current layout?`) && save(w.name)}>
                    Update
                  </button>
                  <button type="button" className="link link-danger" disabled={busy}
                    onClick={() => window.confirm(`Remove the workspace ${w.name}?`) && run(async () => {
                      await deleteWorkspace(w.name);
                      await load();
                    })}>
                    Remove<span className="sr-only"> {w.name}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          <hr />
          <form className="workspace-save" onSubmit={(e) => {
            e.preventDefault();
            if (name.trim()) save(name.trim());
          }}>
            <label className="sr-only" htmlFor={`${id}-name`}>Workspace name</label>
            <input id={`${id}-name`} type="text" maxLength={60} placeholder="e.g. London prep…" value={name}
              onChange={(e) => setName(e.target.value)} />
            <button type="submit" className="button secondary" disabled={busy || !name.trim()}>Save</button>
          </form>
          <p className="field-hint">
            {desktop ? "Saves the chart layout and every window: where it is, its size and its chart."
              : "Saves the chart layout. In the desktop app it keeps the windows and monitors too."}
          </p>
          {message && <p className={message.kind === "error" ? "text-error" : "field-hint"} role="status">{message.text}</p>}
        </div>
      )}
    </div>
  );
}
