"""LLM brief: a short qualitative read of the news to help set the bias.

The dashboard's Settings hold the provider (Claude or OpenAI), the model, the
prompt and a list of news URLs. Generating a brief fetches the text of each URL,
adds the current market context (price, structure per timeframe, the bias in
force) and asks the model for a few points plus a closing line
``BIAS: BULLISH|BEARISH|NEUTRAL``. That line is only a suggestion: the trader
applies it (or not) from the dashboard; nothing is set automatically.

API keys are entered in Settings and kept in the OS credential store
(``secret_store``), never in the database; ``ANTHROPIC_API_KEY`` and
``OPENAI_API_KEY`` in the environment still work. Both SDKs are optional
(``uv sync --extra llm``).
"""

from __future__ import annotations

import importlib.util
import logging
import re
import threading
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser

from .secret_store import get_secret
from .storage import Store

log = logging.getLogger(__name__)

BRIEF_KEY = "brief"
BRIEF_LAST_KEY = "brief_last"

PROVIDERS = {
    "anthropic": {"title": "Claude (Anthropic)", "env": "ANTHROPIC_API_KEY", "secret": "anthropic_api_key",
                  "package": "anthropic",
                  "default_model": "claude-opus-5"},
    "openai": {"title": "OpenAI", "env": "OPENAI_API_KEY", "secret": "openai_api_key", "package": "openai",
               "default_model": "gpt-5"},
}
# Claude models that take server-side refusal fallbacks ("default" routing).
FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5-1")

DEFAULT_PROMPT = """You are a macro analyst helping a discretionary XAUUSD (gold) trader set a directional bias for the coming session.

Use only the news sources and market context provided. Gold usually moves against the US dollar (DXY) and US real yields, and rises on risk-off; US data (CPI, NFP, PCE, FOMC, jobless claims, ISM) and Fed speakers drive most of it. Say when the sources are thin or contradict each other instead of filling gaps.

Reply in plain text:
- 3 to 6 short bullet points: what matters for gold today and why (USD, yields, risk sentiment, upcoming US events with their times if the sources give them).
- One line on what would change the view.
- A final line, exactly one of: BIAS: BULLISH, BIAS: BEARISH, BIAS: NEUTRAL"""

MAX_URLS = 12
_BIAS_LINE = re.compile(r"BIAS:\s*(BULLISH|BEARISH|NEUTRAL)", re.IGNORECASE)


class BriefError(RuntimeError):
    pass


@dataclass
class BriefSettings:
    provider: str = "anthropic"
    model: str | None = None  # None = the provider's default
    prompt: str | None = None  # None = DEFAULT_PROMPT
    urls: list[str] = field(default_factory=list)
    max_chars_per_source: int = 20_000

    @property
    def resolved_model(self) -> str:
        return self.model or PROVIDERS[self.provider]["default_model"]

    @property
    def resolved_prompt(self) -> str:
        return self.prompt or DEFAULT_PROMPT

    def validate(self) -> list[str]:
        errors = []
        if self.provider not in PROVIDERS:
            errors.append(f"Unknown provider {self.provider!r}")
        if len(self.urls) > MAX_URLS:
            errors.append(f"At most {MAX_URLS} URLs")
        errors += [f"Not an http(s) URL: {u}" for u in self.urls if not re.match(r"^https?://\S+$", u)]
        if not 1_000 <= self.max_chars_per_source <= 200_000:
            errors.append("Characters per source must be between 1,000 and 200,000")
        return errors

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> BriefSettings:
        d = d or {}
        base = cls()
        urls = d.get("urls", base.urls)
        return cls(
            provider=d.get("provider") or base.provider,
            model=(d.get("model") or "").strip() or None,
            prompt=(d.get("prompt") or "").strip() or None,
            urls=[u.strip() for u in urls if u and u.strip()],
            max_chars_per_source=int(d.get("max_chars_per_source") or base.max_chars_per_source),
        )


def provider_status() -> list[dict]:
    """Per provider: is its SDK installed, is its API key set and where it comes from (never the key)."""
    out = []
    for pid, info in PROVIDERS.items():
        source = get_secret(info["secret"])[1]
        out.append({"id": pid, "title": info["title"], "default_model": info["default_model"], "env": info["env"],
                    "secret": info["secret"], "installed": importlib.util.find_spec(info["package"]) is not None,
                    "key_set": source is not None, "key_source": source})
    return out


# ---- news sources ---------------------------------------------------------

class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def fetch_source(url: str, max_chars: int, timeout: float = 15) -> dict:
    """Text of one news page. Pages longer than ``max_chars`` are cut and marked as such."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Wednesday news brief)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(5_000_000)
            charset = resp.headers.get_content_charset() or "utf-8"
            ctype = resp.headers.get_content_type()
    except urllib.error.HTTPError as exc:
        return {"url": url, "ok": False, "error": f"HTTP {exc.code}", "chars": 0, "truncated": False, "text": ""}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"url": url, "ok": False, "error": f"Can't reach it: {getattr(exc, 'reason', exc)}", "chars": 0,
                "truncated": False, "text": ""}
    body = raw.decode(charset, errors="replace")
    text = html_to_text(body) if "html" in ctype or body.lstrip()[:1] == "<" else body.strip()
    truncated = len(text) > max_chars
    return {"url": url, "ok": bool(text), "error": None if text else "No readable text", "chars": len(text),
            "truncated": truncated, "text": text[:max_chars]}


def build_input(context: dict, sources: list[dict]) -> str:
    lines = ["Market context (from the screener):"]
    lines += [f"- {k}: {v}" for k, v in context.items()]
    lines.append("")
    readable = [s for s in sources if s["ok"]]
    if not readable:
        lines.append("No news source could be read.")
    for s in readable:
        cut = " (cut to the first part of the page)" if s["truncated"] else ""
        lines.append(f'<source url="{s["url"]}"{cut}>\n{s["text"]}\n</source>')
    return "\n".join(lines)


def suggested_bias(text: str) -> str | None:
    found = _BIAS_LINE.findall(text or "")
    return found[-1].lower() if found else None


# ---- providers --------------------------------------------------------------

def ask_claude(model: str, system: str, user: str) -> str:
    import anthropic

    # The key from Settings; without one the SDK falls back to ANTHROPIC_API_KEY.
    client = anthropic.Anthropic(api_key=get_secret("anthropic_api_key")[0])
    try:
        if model in FALLBACK_MODELS:
            # If the model declines, the API re-runs the request on a fallback model in the same call.
            response = client.beta.messages.create(
                model=model, max_tokens=16000, system=system, messages=[{"role": "user", "content": user}],
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            )
        else:
            response = client.messages.create(
                model=model, max_tokens=16000, system=system, messages=[{"role": "user", "content": user}],
            )
    except anthropic.AuthenticationError as exc:
        raise BriefError("Claude rejected the API key. Check it in Settings > News brief") from exc
    except anthropic.NotFoundError as exc:
        raise BriefError(f"Unknown Claude model {model!r}") from exc
    except anthropic.RateLimitError as exc:
        raise BriefError("Claude rate limit hit; try again in a minute") from exc
    except anthropic.APIStatusError as exc:
        raise BriefError(f"Claude API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise BriefError("Can't reach the Claude API") from exc
    if response.stop_reason == "refusal":
        raise BriefError("Claude declined to write this brief")
    text = "\n".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise BriefError("Claude returned no text")
    return text


def ask_openai(model: str, system: str, user: str) -> str:
    import openai

    client = openai.OpenAI(api_key=get_secret("openai_api_key")[0])
    try:
        response = client.responses.create(model=model, instructions=system, input=user)
    except openai.AuthenticationError as exc:
        raise BriefError("OpenAI rejected the API key. Check it in Settings > News brief") from exc
    except openai.NotFoundError as exc:
        raise BriefError(f"Unknown OpenAI model {model!r}") from exc
    except openai.RateLimitError as exc:
        raise BriefError("OpenAI rate limit hit; try again in a minute") from exc
    except openai.APIStatusError as exc:
        raise BriefError(f"OpenAI API error {exc.status_code}: {exc.message}") from exc
    except openai.APIConnectionError as exc:
        raise BriefError("Can't reach the OpenAI API") from exc
    text = (response.output_text or "").strip()
    if not text:
        raise BriefError("OpenAI returned no text")
    return text


ASK = {"anthropic": ask_claude, "openai": ask_openai}


# ---- runner ---------------------------------------------------------------

class BriefRunner:
    """Generates briefs on a background thread; the dashboard polls :meth:`status`."""

    def __init__(self, store: Store | None, settings: BriefSettings | None = None):
        self.store = store
        self.settings = settings or BriefSettings()
        self.last: dict | None = store.get_setting(BRIEF_LAST_KEY) if store else None
        self.error: str | None = None
        self.running = False
        self._lock = threading.Lock()

    def check_ready(self) -> None:
        s = self.settings
        info = next(p for p in provider_status() if p["id"] == s.provider)
        if not info["installed"]:
            raise BriefError(f"Install the {info['title']} SDK: uv sync --extra llm")
        if not info["key_set"]:
            raise BriefError(f"Add the {info['title']} API key in Settings > News brief")
        if not s.urls:
            raise BriefError("Add at least one news URL")

    def start(self, context: dict) -> None:
        self.check_ready()
        with self._lock:
            if self.running:
                return
            self.running = True
            self.error = None
        threading.Thread(target=self._run, args=(context, self.settings), daemon=True, name="brief").start()

    def generate(self, context: dict, settings: BriefSettings | None = None) -> dict:
        """Fetch the sources and ask the model (blocking)."""
        s = settings or self.settings
        sources = [fetch_source(u, s.max_chars_per_source) for u in s.urls]
        if not any(src["ok"] for src in sources):
            raise BriefError("None of the news URLs could be read")
        text = ASK[s.provider](s.resolved_model, s.resolved_prompt, build_input(context, sources))
        return {
            "text": text,
            "suggested_bias": suggested_bias(text),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "provider": s.provider,
            "model": s.resolved_model,
            "sources": [{k: v for k, v in src.items() if k != "text"} for src in sources],
        }

    def _run(self, context: dict, settings: BriefSettings) -> None:
        try:
            result = self.generate(context, settings)
        except BriefError as exc:
            self.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - report anything unexpected in the dashboard
            log.exception("brief failed")
            self.error = f"{type(exc).__name__}: {exc}"
        else:
            self.last = result
            if self.store:
                self.store.set_setting(BRIEF_LAST_KEY, result)
        finally:
            self.running = False

    def status(self) -> dict:
        return {
            "settings": self.settings.to_dict(),
            "default_prompt": DEFAULT_PROMPT,
            "providers": provider_status(),
            "running": self.running,
            "error": self.error,
            "last": self.last,
        }
