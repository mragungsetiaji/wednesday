import { useId, useState } from "react";

import { initials, useAccount } from "../account";
import { usePopover } from "./usePopover";

const ROLE: Record<string, string> = { owner: "Admin", user: "User", pending: "Waiting for approval" };

const until = (iso: string) =>
  new Date(iso).toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" });

/**
 * The account at the top right, as on other platforms: the avatar opens the account's menu
 * (settings, users for an admin, Log out). In the preview it offers Log in and Sign up. Shows
 * nothing when the server asks for no login (on 127.0.0.1).
 */
export function AccountMenu() {
  const { status, openForm, logOut } = useAccount();
  const { open, setOpen, ref } = usePopover<HTMLDivElement>();
  const [busy, setBusy] = useState(false);
  const id = useId();
  if (!status?.login) return null;

  const user = status.logged_in ? status.user : null;
  if (!user) {
    return (
      <div className="tb-menu account" ref={ref}>
        <button type="button" className="tb-btn account-login" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}
          title="Log in to keep your drawings, templates and layout in your account">
          Log in
        </button>
        {open && (
          <div className="chart-pop menu-pop is-end account-pop" id={id} role="menu" aria-label="Account">
            <p className="account-head">
              <strong>Preview</strong>
              {status.guest && <span>Until {until(status.guest.until)}. Drawings stay in this browser.</span>}
            </p>
            <hr />
            <button type="button" role="menuitem" className="draw-flyout-item" onClick={() => { setOpen(false); openForm("login"); }}>
              Log in
            </button>
            {status.signup !== false && (
              <button type="button" role="menuitem" className="draw-flyout-item" onClick={() => { setOpen(false); openForm("signup"); }}>
                Sign up
              </button>
            )}
          </div>
        )}
      </div>
    );
  }

  const name = user.name || user.username;
  const leave = async () => {
    setBusy(true);
    try {
      await logOut();
    } finally {
      setBusy(false);
      setOpen(false);
    }
  };
  return (
    <div className="tb-menu account" ref={ref}>
      <button type="button" className="tb-btn account-btn" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}
        title={`Logged in as ${user.username}`} aria-label={`Account: ${user.username}`}>
        <span className="avatar" aria-hidden="true">{initials(name)}</span>
      </button>
      {open && (
        <div className="chart-pop menu-pop is-end account-pop" id={id} role="menu" aria-label="Account">
          <div className="account-head">
            <span className="avatar is-big" aria-hidden="true">{initials(name)}</span>
            <span>
              <strong>{name}</strong>
              <span>{user.name ? `${user.username} · ` : ""}{ROLE[user.role] ?? user.role}</span>
            </span>
          </div>
          <hr />
          <a role="menuitem" className="draw-flyout-item" href="#settings/login" onClick={() => setOpen(false)}>Account and devices</a>
          {user.role === "owner" && (
            <a role="menuitem" className="draw-flyout-item" href="#settings/users" onClick={() => setOpen(false)}>Users</a>
          )}
          <hr />
          <button type="button" role="menuitem" className="draw-flyout-item" disabled={busy} onClick={leave}>
            Log out
          </button>
        </div>
      )}
    </div>
  );
}
