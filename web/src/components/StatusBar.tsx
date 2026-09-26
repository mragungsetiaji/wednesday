import { TagIcon } from "../icons";

/** Bottom bar, always on screen: app facts at a glance. */
export function StatusBar({ version }: { version: string | undefined }) {
  return (
    <footer className="statusbar">
      <span className="statusbar-item num" title="Wednesday version">
        <TagIcon size={12} />
        {version ? `v${version}` : "—"}
      </span>
    </footer>
  );
}
