import { useEffect, useState, type ReactNode } from "react";

import { acceptTerms, fetchTerms, type TermsResponse } from "../api";

/** Just enough Markdown for DISCLAIMER.md: headings, bullet lists, paragraphs and [links](url). */
function Markdown({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  let para: string[] = [];
  let items: string[] = [];
  const inline = (s: string) => s.replace(/\[([^\]]+)\]\([^)]+\)/g, "$1").replace(/\*\*([^*]+)\*\*/g, "$1");
  const flush = () => {
    if (para.length) blocks.push(<p key={blocks.length}>{inline(para.join(" "))}</p>);
    if (items.length) blocks.push(<ul key={blocks.length}>{items.map((t, i) => <li key={i}>{inline(t)}</li>)}</ul>);
    para = [];
    items = [];
  };
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (line.startsWith("# ")) continue; // the dialog has its own title
    if (line.startsWith("## ")) {
      flush();
      blocks.push(<h3 key={blocks.length}>{line.slice(3)}</h3>);
    } else if (line.startsWith("- ")) {
      if (para.length) flush();
      items.push(line.slice(2));
    } else if (line === "") {
      flush();
    } else if (items.length) {
      items[items.length - 1] += ` ${line}`; // a wrapped list item
    } else {
      para.push(line);
    }
  }
  flush();
  return <>{blocks}</>;
}

/**
 * Shows the disclaimer and risk agreement until it is accepted (once per version of the terms),
 * then the app. If the API can't be reached the app shows anyway and reports it as offline.
 */
export function TermsGate({ children }: { children: ReactNode }) {
  const [terms, setTerms] = useState<TermsResponse | null>(null);
  const [failed, setFailed] = useState(false);
  const [agree, setAgree] = useState(false);
  const [declined, setDeclined] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchTerms().then(setTerms).catch(() => setFailed(true));
  }, []);

  if (failed || terms?.accepted) return <>{children}</>;
  if (!terms) return null;

  const accept = async () => {
    setBusy(true);
    setError(null);
    try {
      setTerms(await acceptTerms());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="terms-page">
      <section className="terms-card" role="dialog" aria-modal="true" aria-labelledby="terms-h">
        <h2 id="terms-h">Disclaimer and risk agreement</h2>
        <p className="terms-lead">Please read this before using Wednesday.</p>
        <div className="terms-text">
          <Markdown text={terms.text} />
        </div>
        {declined ? (
          <p className="notice">
            Wednesday can't be used without accepting these terms. Close the app, or accept them to continue.
          </p>
        ) : null}
        <label className="check terms-check">
          <input type="checkbox" checked={agree} onChange={(e) => { setAgree(e.target.checked); setDeclined(false); }} />
          I have read this. I understand Wednesday is not financial advice, and that every trading decision and every loss
          is my own responsibility.
        </label>
        <div className="form-actions">
          <button type="button" className="button primary" disabled={!agree || busy} onClick={accept}>
            {busy ? "Saving…" : "I accept"}
          </button>
          <button type="button" className="button quiet" disabled={busy} onClick={() => { setDeclined(true); setAgree(false); }}>
            I don't accept
          </button>
          {error && <span className="form-status text-error" role="status">{error}</span>}
        </div>
      </section>
    </div>
  );
}
