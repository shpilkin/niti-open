"""Cleaning: HTML -> text, strip quoted replies and signatures.

Heuristic and conservative: better to keep a bit of noise than to cut real content.
Works for RU and EN conventions.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, NavigableString

# Lines like "On Mon, ... wrote:" / "В 10:00 ... пишет:" introducing a quoted reply.
_QUOTE_HEADER_RE = re.compile(
    r"^\s*(on\s.+wrote:|в\s.+(пишет|написал|writes):?|.*\bписьм[еа]\b.+:)\s*$",
    re.IGNORECASE,
)
# Signature delimiters.
_SIG_DELIM_RE = re.compile(r"^\s*--\s*$")
_SIG_PHRASE_RE = re.compile(
    r"^\s*(с уважением|best regards|kind regards|regards|sent from my|"
    r"отправлено с|с наилучшими пожеланиями)\b",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"[ \t]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")


# A line break belongs to the structure of the document, not to every tag: mail from
# Outlook wraps pieces of one sentence in <span lang="EN-US">, so joining text nodes with
# "\n" turned «ivanov@corpmail.ru» into five lines. Markers go where a browser would break;
# a run of them collapses into one break instead of one per tag.
_LINE = "\ue000"  # ends the line: div, list item, table cell
_PARA = "\ue001"  # blank line around it: paragraph-level block
_BR = "\ue002"

_LINE_TAGS = ["dd", "div", "dt", "li", "option", "td", "th", "tr"]
_PARA_TAGS = [
    "address", "article", "aside", "blockquote", "fieldset", "figcaption", "figure",
    "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "main",
    "nav", "ol", "p", "pre", "section", "table", "ul",
]
_SOURCE_WS_RE = re.compile(r"\s+")
_BREAK_RUN_RE = re.compile(r"[ \t]*[\ue000-\ue002][ \t\ue000-\ue002]*")


def _collapse_break(match: re.Match[str]) -> str:
    run = match.group(0)
    return "\n\n" if _PARA in run or run.count(_BR) > 1 else "\n"


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "head", "title"]):
        tag.decompose()
    for node in soup.find_all(string=True):
        # Exact type: Comment is a NavigableString too, and replacing one with plain text
        # would drag «<!--[if mso]>…» into the body. Whitespace in the source — newlines
        # included — is layout of the markup, not of the text.
        if type(node) is NavigableString:
            node.replace_with(_SOURCE_WS_RE.sub(" ", node))
    for br in soup.find_all("br"):
        br.replace_with(_BR)
    for tag in soup.find_all(_LINE_TAGS):
        tag.insert_before(_LINE)
        tag.insert_after(_LINE)
    for tag in soup.find_all(_PARA_TAGS):
        tag.insert_before(_PARA)
        tag.insert_after(_PARA)
    return _BREAK_RUN_RE.sub(_collapse_break, soup.get_text("")).strip()


def strip_quotes_and_signature(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    for line in lines:
        if _QUOTE_HEADER_RE.match(line):
            break  # everything below is the quoted original
        if _SIG_DELIM_RE.match(line) or _SIG_PHRASE_RE.match(line):
            break  # signature -> stop
        if line.lstrip().startswith(">"):
            continue  # quoted line
        out.append(line)
    return "\n".join(out)


def clean_body(body_text: str | None, body_html: str | None) -> str:
    raw = body_text
    if not raw and body_html:
        raw = html_to_text(body_html)
    if not raw:
        return ""
    cleaned = strip_quotes_and_signature(raw)
    cleaned = "\n".join(_WS_RE.sub(" ", ln).rstrip() for ln in cleaned.splitlines())
    cleaned = _MULTI_NL_RE.sub("\n\n", cleaned).strip()
    return cleaned
