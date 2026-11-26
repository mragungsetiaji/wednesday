import { createContext, useContext } from "react";

import type { AuthStatus } from "./api";

/** Who is using the dashboard, as the login gate knows it, and its ways in and out. */
export interface Account {
  status: AuthStatus | null; // null: no login check (the API is unreachable)
  openForm: (mode: "login" | "signup") => void; // the login or sign-up form over the preview
  logOut: () => Promise<void>;
}

export const AccountContext = createContext<Account>({ status: null, openForm: () => {}, logOut: async () => {} });

export const useAccount = () => useContext(AccountContext);

/** "AS" for "A. Setiaji", "AJ" for "aji": the avatar's letters. */
export function initials(name: string): string {
  const words = name.trim().split(/[\s._-]+/).filter(Boolean);
  if (!words.length) return "?";
  const letters = words.length > 1 ? words[0][0] + words[words.length - 1][0] : words[0].slice(0, 2);
  return letters.toUpperCase();
}
