import type { SecretSource } from "../api";

const SOURCE_TEXT: Record<string, string> = {
  saved: "Saved in the system credential store (Windows Credential Manager).",
  session: "Kept until the app closes: this PC has no credential store.",
  env: "Read from .env.",
};

/**
 * A write-only secret input: the stored value never comes back from the API, so the box stays
 * empty and says whether one is saved. Typing replaces it on save; "Forget" removes it.
 */
export function SecretField({ label, source, value, forget, placeholder, onChange, onForget }: {
  label: string;
  source: SecretSource;
  value: string;
  forget: boolean;
  placeholder: string;
  onChange: (v: string) => void;
  onForget: () => void;
}) {
  const has = source !== null && !forget;
  return (
    <div className="field">
      <label className="field">
        <span className="field-label">{label}</span>
        <input type="password" autoComplete="new-password" spellCheck={false} value={value}
          placeholder={has ? "•••••••• (leave empty to keep)" : placeholder}
          onChange={(e) => onChange(e.target.value)} />
      </label>
      <span className="field-hint">
        {forget ? "Will be removed when you save." : source ? SOURCE_TEXT[source] : "Kept in the system credential store, never in the database."}
        {has && source !== "env" && (
          <>
            {" "}
            <button type="button" className="link-button" onClick={onForget}>Forget it</button>
          </>
        )}
      </span>
    </div>
  );
}
