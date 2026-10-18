"""Shared helpers for the daily-learning pipeline.

Standard library only: the GitHub Actions runner installs nothing.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # 仓库根目录 hanlinli666.github.io
APP_DIR = ROOT / "daily"                        # PWA 本体，部署在 /daily/
DATA_DIR = APP_DIR / "data"
CONFIG_DIR = APP_DIR / "config"
CACHE_DIR = DATA_DIR / "cache"
KEYWORDS_FILE = APP_DIR / "keywords.txt"

# Everything the reader sees is dated in Beijing time (no DST in Asia/Shanghai).
CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

USER_AGENT = (
    "daily-learning/1.0 (+https://github.com/; personal daily digest bot)"
)

TAG = {"papers": "papers", "news": "news", "feishu": "feishu"}


def _use_utf8_stdout() -> None:
    """Windows consoles default to GBK and explode on emoji - force UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - older interpreters / exotic streams
            pass


_use_utf8_stdout()


def log(kind: str, message: str) -> None:
    stamp = datetime.now(CN_TZ).strftime("%H:%M:%S")
    print(f"[{stamp}] [{kind}] {message}", flush=True)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_dt(value: str | None) -> datetime | None:
    """Parse the many date shapes found in RSS feeds. Returns aware UTC or None."""
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None

    # RFC 822 / RFC 1123, e.g. "Sun, 18 Oct 2026 08:00:00 +0000" or "... GMT"
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M %z",
        "%d %b %Y %H:%M:%S %z",
    ):
        try:
            dt = datetime.strptime(raw.replace("GMT", "+0000").replace("GMT+0000", "+0000"), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue

    # ISO 8601
    candidate = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass

    # Trim trailing timezone names / extra tokens and retry
    trimmed = re.sub(r"\s+[A-Z]{2,5}$", "", raw)
    if trimmed != raw:
        return parse_dt(trimmed)
    return None


def cn_day(dt: datetime) -> str:
    """Beijing calendar day as YYYY-MM-DD."""
    return dt.astimezone(CN_TZ).strftime("%Y-%m-%d")


def _decode_body(raw: bytes, charset_hint: str | None = None) -> str:
    """Decode bytes, honouring the XML/HTML declared charset, guessing as a fallback."""
    head = raw[:600].decode("ascii", "ignore")
    declared = re.search(r'encoding=["\']([\w\-]+)["\']', head)
    meta = re.search(r'charset=["\']?([\w\-]+)', head)
    candidates = [
        charset_hint,
        declared.group(1) if declared else None,
        meta.group(1) if meta else None,
        "utf-8",
    ]
    for enc in candidates:
        if not enc:
            continue
        enc = enc.strip().lower()
        if enc in ("gb2312", "gbk"):
            enc = "gb18030"
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def http_get(
    url: str,
    *,
    timeout: float = 30.0,
    retries: int = 3,
    headers: dict[str, str] | None = None,
    accept: str = "*/*",
) -> str:
    """GET a URL and return decoded text. Retries on transient failures."""
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": accept,
                    "Accept-Encoding": "gzip, deflate",
                    **(headers or {}),
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                elif resp.headers.get("Content-Encoding", "").lower() == "deflate":
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                return _decode_body(raw, resp.headers.get_content_charset())
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code in (400, 401, 403, 404, 410):
                raise
        except Exception as exc:  # noqa: BLE001 - network flakiness is expected
            last_error = exc
        if attempt < retries:
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"GET failed after {retries} attempts: {url} :: {last_error}")


def http_post_json(
    url: str,
    payload: dict,
    *,
    timeout: float = 30.0,
    retries: int = 3,
    headers: dict[str, str] | None = None,
) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "User-Agent": USER_AGENT,
                    **(headers or {}),
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = _decode_body(resp.read(), resp.headers.get_content_charset())
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"_raw": text}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            last_error = RuntimeError(f"HTTP {exc.code}: {detail}")
            # 401/403/404 means the key, model or URL is wrong: retrying wastes time.
            if exc.code in (400, 401, 403, 404):
                raise last_error from None
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        if attempt < retries:
            time.sleep(2.0 * attempt)
    raise RuntimeError(f"POST failed after {retries} attempts: {url} :: {last_error}")


def read_json(path: Path, default):
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(value, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    log("io", f"wrote {path.relative_to(ROOT)} ({path.stat().st_size:,} bytes)")


def load_local_env() -> None:
    """Load KEY=VALUE lines from .env.local / config/local.env (never committed)."""
    for candidate in (ROOT / ".env.local", CONFIG_DIR / "local.env"):
        if not candidate.is_file():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value


def collapse(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def strip_html(text: str | None) -> str:
    """Turn an RSS summary blob into readable plain text."""
    if not text:
        return ""
    value = str(text)
    value = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", value)
    value = re.sub(r"(?i)<br\s*/?>", " ", value)
    value = re.sub(r"(?i)</p\s*>", " ", value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = (
        value.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&apos;", "'")
        .replace("&mdash;", "—")
        .replace("&ndash;", "–")
        .replace("&hellip;", "…")
    )
    value = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), value)
    value = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), value)
    return collapse(value)


def truncate(text: str, limit: int) -> str:
    text = collapse(text)
    if len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut + "…"


def guess_bj_day(dt: datetime | None) -> str:
    return cn_day(dt or now_utc())


def fail(message: str, code: int = 1) -> None:
    log("error", message)
    sys.exit(code)
