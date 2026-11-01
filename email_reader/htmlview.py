"""Safe rendering of original email HTML.

Emails keep their original `body_raw_html`. To show it without exposing the app to
scripts/trackers we (1) sanitize with nh3 (strips scripts, event handlers, iframes,
forms, etc.) and (2) render the result in a sandboxed iframe whose Content-Security-
Policy blocks all active content and (by default) remote images.
"""

from __future__ import annotations

import re
from html import unescape

import nh3

# Signatures are user-authored rich HTML (pasted/typed), so we KEEP formatting —
# inline styles (color/font), <font>, links, images, lists — but still strip scripts,
# event handlers, iframes, forms (nh3 does this regardless of the allowlist).
_SIG_TAGS = {
    "a", "b", "strong", "i", "em", "u", "s", "span", "div", "p", "br", "font",
    "ul", "ol", "li", "h1", "h2", "h3", "h4", "small", "sub", "sup", "blockquote", "img",
}
_SIG_ATTRS = {
    "a": {"href", "title", "style"},
    "font": {"color", "face", "size", "style"},
    "img": {"src", "alt", "width", "height", "style"},
    "span": {"style"}, "div": {"style"}, "p": {"style"}, "li": {"style"},
    "b": {"style"}, "i": {"style"}, "u": {"style"}, "strong": {"style"}, "em": {"style"},
    "h1": {"style"}, "h2": {"style"}, "h3": {"style"}, "h4": {"style"},
    "blockquote": {"style"},
}


def sanitize_signature_html(html: str | None) -> str:
    if not html:
        return ""
    return nh3.clean(
        html, tags=_SIG_TAGS, attributes=_SIG_ATTRS,
        link_rel="noopener noreferrer nofollow",
    ).strip()


def signature_to_text(html: str | None) -> str:
    """Plain-text version of a rich signature, for the text/plain email part."""
    if not html:
        return ""
    t = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    t = re.sub(r"</(p|div|li|h[1-4]|blockquote)>", "\n", t, flags=re.I)
    # nh3 снимает теги, но оставляет сущности как есть, и «&nbsp;» уезжало прямо в
    # text/plain отправленного письма — получатель видел его буквами. Неразрывный пробел
    # в текстовой части не нужен: там его роль играет обычный.
    t = unescape(nh3.clean(t, tags=set()))
    return t.replace("\u00a0", " ").strip()


_FONT_FAMILY_RE = re.compile(r"font-family:\s*([^;]+)", re.I)
_FONT_SIZE_RE = re.compile(r"font-size:\s*(\d+(?:\.\d+)?(?:pt|px|em|%))", re.I)


def font_of(html: str | None) -> tuple[str | None, str | None]:
    """Шрифт и размер, которые объявляет подпись, — ими же набирается письмо.

    Сущности раскрываем до разбора: в «font-family:&quot;Calibri&quot;» точка с запятой
    внутри &quot; обрывала совпадение на полуслове."""
    if not html:
        return None, None
    src = unescape(html)
    family = _FONT_FAMILY_RE.search(src)
    size = _FONT_SIZE_RE.search(src)
    face = ""
    if family:
        face = family.group(1).replace('"', "").replace("'", "").strip().rstrip(",")
    return (face or None), (size.group(1) if size else None)


def html_to_text(raw: str | None) -> str:
    """Текст письма из его разметки — для цитаты в ответе.

    Нужен потому, что text/plain у писем часто короче: клиенты кладут туда только
    последнее сообщение, а всю переписку несёт text/html. Цитата из короткой части
    обрывала цепочку."""
    if not raw:
        return ""
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|blockquote)>", "\n", t)
    t = re.sub(r"(?i)</t[dh]>", "\t", t)
    t = unescape(nh3.clean(t, tags=set()))
    t = t.replace("\u00a0", " ")
    t = re.sub(r"[ \t]+\n", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


# Свойства, которые письму оставляем: оформление и раскладка. Всё, чем можно накрыть
# страницу или утащить данные (position, z-index, behavior, url()), nh3 отбросит сам —
# он пропускает только перечисленные имена.
_MAIL_STYLE_PROPS = frozenset({
    # background (сокращение) не берём: через него письмо тянет картинку-счётчик
    # мимо <img>. Цвет фона отдельным свойством безопасен.
    "color", "background-color",
    "font", "font-family", "font-size", "font-style", "font-weight", "font-variant",
    "line-height", "letter-spacing", "text-align", "text-decoration", "text-indent",
    "text-transform", "vertical-align", "white-space", "direction",
    "margin", "margin-top", "margin-right", "margin-bottom", "margin-left",
    "padding", "padding-top", "padding-right", "padding-bottom", "padding-left",
    "border", "border-top", "border-right", "border-bottom", "border-left",
    "border-collapse", "border-color", "border-style", "border-width", "border-radius",
    "width", "min-width", "max-width", "height", "list-style-type",
})
_MAIL_ATTRS = {
    **{tag: set(attrs) for tag, attrs in nh3.ALLOWED_ATTRIBUTES.items()},
    "*": {*nh3.ALLOWED_ATTRIBUTES.get("*", set()), "style"},
}


_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
# Outlook прячет содержимое <style> в HTML-комментарий — ради почтовиков, не знавших
# про <style>. Без снятия обёртки первое правило склеивается с «<!--» и отбраковывается
# как невалидный селектор, а первое правило у Word как раз и задаёт шрифт письма.
_CSS_HTML_COMMENT_RE = re.compile(r"<!--|-->")
_STYLE_BLOCK_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.S | re.I)


def _strip_at_rules(css: str) -> str:
    """Выбросить @font-face, @page, @list, @media вместе с их телом.

    Инлайнить их некуда: они описывают не элементы, а шрифты, страницу и условия."""
    out, i = [], 0
    while i < len(css):
        at = css.find("@", i)
        if at == -1:
            out.append(css[i:])
            break
        out.append(css[i:at])
        brace = css.find("{", at)
        if brace == -1:
            break
        depth, j = 1, brace + 1
        while j < len(css) and depth:
            depth += (css[j] == "{") - (css[j] == "}")
            j += 1
        i = j
    return "".join(out)


def _split_decls(decls: str) -> list[tuple[str, str]]:
    """Разбор `свойство: значение; …`. Точка с запятой внутри скобок — часть значения
    (`url("data:image/png;base64,…")`), а не разделитель."""
    out, depth, start = [], 0, 0
    for i, ch in enumerate(decls):
        depth += (ch == "(") - (ch == ")")
        if ch == ";" and not depth:
            out.append(decls[start:i])
            start = i + 1
    out.append(decls[start:])
    pairs = []
    for chunk in out:
        name, sep, value = chunk.partition(":")
        if sep and name.strip() and value.strip():
            pairs.append((name.strip().lower(), value.strip()))
    return pairs


def _inline_style_blocks(html: str) -> str:
    """Правила из `<style>` — в атрибуты элементов, пока `<style>` ещё цел.

    Word и Outlook задают шрифт классами (`p.MsoNormal{font-family:Calibri}`), а
    санитайзер вырезает и `<style>`, и `class`: письмо приходило системным шрифтом, и
    цитата в ответе теряла шрифт оригинала. Втягиваем только те свойства, что и так
    переживут санитайзер, — остальные он срежет, а разметку раздуют.

    Порядок приближённый: поздний бьёт раннего, инлайновый бьёт всех. Специфичность не
    считаем — Word выдаёт правила уже в нужном порядке, и на письмах это совпадает."""
    if "<style" not in html.lower():
        return html
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    css = _CSS_HTML_COMMENT_RE.sub(
        " ", _CSS_COMMENT_RE.sub("", "\n".join(_STYLE_BLOCK_RE.findall(html)))
    )
    from_css: dict[int, dict[str, str]] = {}
    tags: dict[int, object] = {}
    for chunk in _strip_at_rules(css).split("}"):
        selector, sep, decls = chunk.partition("{")
        if not sep or not selector.strip():
            continue
        wanted = [(n, v) for n, v in _split_decls(decls) if n in _MAIL_STYLE_PROPS]
        if not wanted:
            continue
        try:
            found = soup.select(selector.strip())
        except Exception:  # noqa: BLE001 — кривой селектор роняет правило, не письмо
            continue
        for tag in found:
            props = from_css.setdefault(id(tag), {})
            tags[id(tag)] = tag
            props.update(wanted)
    for key, props in from_css.items():
        tag = tags[key]
        own = dict(_split_decls(tag.get("style") or ""))
        merged = {**props, **own}  # написанное на элементе побеждает правило из <style>
        tag["style"] = ";".join(f"{n}:{v}" for n, v in merged.items())
    return str(soup)


# Картинка, вложенная в само письмо: <img src="cid:image001.png@01DD4CD3.5381F860">.
_CID_SRC_RE = re.compile(r"""(\bsrc\s*=\s*)(["']?)cid:([^"'\s>]+)\2""", re.I)
_REMOTE_IMG_RE = re.compile(r"""<img\b[^>]*\bsrc\s*=\s*["']?\s*https?:""", re.I)
_IMG_RE = re.compile(r"<img\b", re.I)


def cid_refs(html: str | None) -> set[str]:
    """Какие вложенные картинки письмо показывает (Content-ID без скобок)."""
    return {m.group(3) for m in _CID_SRC_RE.finditer(html or "")}


def has_remote_images(html: str | None) -> bool:
    """Есть ли картинки из интернета — только их и прячет кнопка «Показать картинки»."""
    return bool(_REMOTE_IMG_RE.search(html or ""))


def is_blank(html: str | None) -> bool:
    """Разметка без текста и без картинок — так Exchange присылает приглашения."""
    return not html_to_text(html) and not _IMG_RE.search(html or "")


def _only_images_as_data(element: str, attribute: str, value: str) -> str | None:
    """data:-адрес пропускаем только картинкой: ссылка на data:text/html — это страница."""
    if value.lower().startswith("data:") and not (
        element == "img" and attribute == "src" and value.lower().startswith("data:image/")
    ):
        return None
    return value


def sanitize_email_html(html: str | None, inline: dict[str, str] | None = None) -> str:
    """`inline` — вложенные картинки письма: Content-ID → data:-адрес. Подставляются до
    очистки: nh3 выбрасывает cid:-адрес прежде, чем его можно было бы заменить."""
    if not html:
        return ""
    if inline:
        html = _CID_SRC_RE.sub(
            lambda m: f'{m.group(1)}"{inline[m.group(3)]}"' if m.group(3) in inline
            else m.group(0),
            html,
        )
    # nh3 (ammonia) defaults are a curated safe allowlist: keeps formatting, tables,
    # links and images; removes <script>/<style>/<iframe>/forms and on* handlers.
    # Инлайновые стили он по умолчанию срезает вместе со шрифтами и цветами, и письмо
    # приходило чужим шрифтом — поэтому style разрешён, но с фильтром свойств.
    return nh3.clean(
        _inline_style_blocks(html),
        link_rel="noopener noreferrer nofollow",
        attributes=_MAIL_ATTRS,
        filter_style_properties=set(_MAIL_STYLE_PROPS),
        url_schemes=nh3.ALLOWED_URL_SCHEMES | {"data"},
        attribute_filter=_only_images_as_data,
    )


def csp_header(allow_images: bool) -> str:
    img = "img-src http: https: data:;" if allow_images else "img-src data:;"
    return (
        "default-src 'none'; "
        "style-src 'unsafe-inline'; "
        f"{img} "
        "font-src data:; "
        "base-uri 'none'; "
        "form-action 'none'"
    )


def html_document(sanitized_body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='referrer' content='no-referrer'>"
        "<style>body{font:14px -apple-system,Segoe UI,Roboto,sans-serif;"
        "color:#1a1a1a;margin:12px;word-wrap:break-word}"
        "img{max-width:100%;height:auto}a{color:#10a37f}</style></head>"
        f"<body>{sanitized_body}</body></html>"
    )
