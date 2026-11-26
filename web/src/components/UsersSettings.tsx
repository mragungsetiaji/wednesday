import { useEffect, useState } from "react";

import { deleteUser, fetchUsers, updateUser, type UserAccount } from "../api";

const fmtWhen = (iso: string | null) => (iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "–");
const ROLE: Record<UserAccount["role"], string> = { owner: "Admin", user: "User", pending: "Waiting for approval" };

/**
 * Settings > Users (the admin): the accounts, sign-ups waiting for approval first. Approve makes
 * a sign-up a user; Reject removes it. An admin changes the data source, markets, Telegram,
 * bias and licence; a user keeps their own drawings, layout and journals.
 */
export function UsersSettings({ me }: { me: string | null }) {
  const [users, setUsers] = useState<UserAccount[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: "error" | "ok"; text: string } | null>(null);

  useEffect(() => {
    fetchUsers()
      .then((r) => setUsers(r.users))
      .catch((e) => setMessage({ kind: "error", text: `Can't load the accounts: ${e instanceof Error ? e.message : e}` }));
  }, []);

  const run = async (work: () => Promise<{ users: UserAccount[] }>, done: string) => {
    setBusy(true);
    setMessage(null);
    try {
      setUsers((await work()).users);
      setMessage({ kind: "ok", text: done });
    } catch (e) {
      setMessage({ kind: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const remove = (u: UserAccount) => {
    const what = u.role === "pending" ? `Reject the sign-up of ${u.username}?` :
      `Remove ${u.username}? Their drawings, journals and settings are deleted too.`;
    if (window.confirm(what)) void run(() => deleteUser(u.id), u.role === "pending" ? `Rejected ${u.username}.` : `Removed ${u.username}.`);
  };

  const order = (u: UserAccount) => (u.role === "pending" ? 0 : 1);
  const sorted = [...(users ?? [])].sort((a, b) => order(a) - order(b) || a.username.localeCompare(b.username));
  const waiting = sorted.filter((u) => u.role === "pending").length;

  return (
    <section className="settings-form" aria-labelledby="users-h">
      <div className="settings-intro">
        <h2 id="users-h">Users</h2>
        <p>
          Anyone can sign up from the login screen; an account logs in once you approve it. Users keep their own drawings,
          layout and journals. Only an admin changes the data source, markets, Telegram alerts, the bias and the licence.
        </p>
        {waiting > 0 && <p><strong>{waiting === 1 ? "1 sign-up waits" : `${waiting} sign-ups wait`} for approval.</strong></p>}
      </div>

      {message && <p className={message.kind === "error" ? "text-error" : "muted"} role={message.kind === "error" ? "alert" : "status"}>{message.text}</p>}

      {users && (
        <table className="data compact users-table">
          <thead>
            <tr>
              <th scope="col">Username</th>
              <th scope="col">Email</th>
              <th scope="col">Role</th>
              <th scope="col">Signed up</th>
              <th scope="col">Last login</th>
              <th scope="col"><span className="visually-hidden">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((u) => {
              const self = u.username === me;
              return (
                <tr key={u.id}>
                  <td>{u.username}{self && <span className="muted"> (you)</span>}</td>
                  <td>{u.email ?? "–"}</td>
                  <td>{ROLE[u.role] ?? u.role}{u.disabled && <span className="text-error"> · disabled</span>}</td>
                  <td>{fmtWhen(u.created_at)}</td>
                  <td>{fmtWhen(u.last_login_at)}</td>
                  <td className="row-actions">
                    {u.role === "pending" ? (
                      <button type="button" className="button primary" disabled={busy}
                        onClick={() => void run(() => updateUser(u.id, { role: "user" }), `Approved ${u.username}.`)}>
                        Approve
                      </button>
                    ) : !self && (
                      <>
                        <button type="button" className="button quiet" disabled={busy}
                          onClick={() => void run(() => updateUser(u.id, { role: u.role === "owner" ? "user" : "owner" }),
                            u.role === "owner" ? `${u.username} is a user now.` : `${u.username} is an admin now.`)}>
                          {u.role === "owner" ? "Make user" : "Make admin"}
                        </button>
                        <button type="button" className="button quiet" disabled={busy}
                          onClick={() => void run(() => updateUser(u.id, { disabled: !u.disabled }),
                            u.disabled ? `${u.username} can log in again.` : `${u.username} is disabled and logged out.`)}>
                          {u.disabled ? "Enable" : "Disable"}
                        </button>
                      </>
                    )}
                    {!self && (
                      <button type="button" className="button quiet" disabled={busy} onClick={() => remove(u)}>
                        {u.role === "pending" ? "Reject" : "Remove"}
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}
