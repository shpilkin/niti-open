# ruff: noqa: E501 — SVG path data is a single unbreakable string per icon.
"""Monochrome line icons (ChatGPT-style) rendered as inline SVG.

Registered as the Jinja global ``icon(name)`` so templates can do
``{{ icon('mail') }}``. Icons inherit color via ``currentColor`` and size via
``1em`` (they scale with the container's ``font-size``), so no per-icon color —
this replaces the old colored emoji.
"""

from __future__ import annotations

from markupsafe import Markup

# Each value is the inner SVG markup. The wrapper sets fill=none +
# stroke=currentColor; filled icons (star) override per-path.
_ICONS: dict[str, str] = {
    # settings tabs
    "mail": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3.5 7.5l8.5 6 8.5-6"/>',
    "tag": '<path d="M3 3h7l11 11-7 7L3 10V3z"/><circle cx="7.5" cy="7.5" r="1.3"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4.5 20.5c0-4 3.5-6 7.5-6s7.5 2 7.5 6"/>',
    "gear": '<circle cx="12" cy="12" r="3.2"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3'
            'M5 5l2.1 2.1M16.9 16.9L19 19M19 5l-2.1 2.1M7.1 16.9L5 19"/>',
    "database": '<ellipse cx="12" cy="5.5" rx="7.5" ry="3"/>'
                '<path d="M4.5 5.5v13c0 1.6 3.4 3 7.5 3s7.5-1.4 7.5-3v-13"/>'
                '<path d="M4.5 12c0 1.6 3.4 3 7.5 3s7.5-1.4 7.5-3"/>',
    "prompt": '<path d="M4 5a1.5 1.5 0 011.5-1.5h13A1.5 1.5 0 0120 5v9a1.5 1.5 0 01-1.5 1.5H9l-5 4.5z"/>'
              '<path d="M8 8h8M8 11.5h5"/>',
    "lock": '<rect x="4.5" y="10" width="15" height="10.5" rx="2"/><path d="M8 10V7a4 4 0 018 0v3"/>',
    "bell": '<path d="M6 9.5a6 6 0 1112 0c0 4.5 2 6.5 2 6.5H4s2-2 2-6.5z"/><path d="M10 20a2 2 0 004 0"/>',
    "appearance": '<circle cx="12" cy="12" r="9"/><path fill="currentColor" stroke="none" d="M12 3a9 9 0 010 18z"/>',
    # providers / misc
    "globe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3.2 3 14.8 0 18M12 3c-3 3.2-3 14.8 0 18"/>',
    "star": '<path fill="currentColor" stroke="none" d="M12 2.2c.55 4.6 1.65 5.7 6.25 6.25'
            'C13.65 9 12.55 10.1 12 14.7 11.45 10.1 10.35 9 5.75 8.45 10.35 7.9 11.45 6.8 12 2.2z"/>',
    "paperclip": '<path d="M21.44 11.05l-9.19 9.19a5 5 0 01-7.07-7.07l9.19-9.19a3.5 3.5 0 014.95 4.95'
                 'l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.49"/>',
    "pen": '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 013 3L7 19l-4 1 1-4 12.5-12.5z"/>',
    # action / nav
    "check": '<path d="M5 12.5l4.5 4.5L19 7"/>',
    "x": '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
    # листать неделю календаря назад и вперёд; место встречи
    "chevron-left": '<path d="M14.5 6.5L9 12l5.5 5.5"/>',
    "chevron-right": '<path d="M9.5 6.5L15 12l-5.5 5.5"/>',
    "pin": '<path d="M12 21s-6.5-5.6-6.5-11a6.5 6.5 0 0113 0c0 5.4-6.5 11-6.5 11z"/>'
           '<circle cx="12" cy="10" r="2.3"/>',
    # два наложенных квадрата — общепринятый знак «скопировать»
    "copy": '<rect x="9" y="9" width="12" height="12" rx="2"/>'
            '<path d="M5 15H4.5A1.5 1.5 0 013 13.5v-9A1.5 1.5 0 014.5 3h9A1.5 1.5 0 0115 4.5V5"/>',
    "reply": '<path d="M9.5 8L5 12.5l4.5 4.5"/><path d="M5 12.5h9a5.5 5.5 0 015.5 5.5V19"/>',
    "forward": '<path d="M14.5 8l4.5 4.5-4.5 4.5"/><path d="M19 12.5h-9A5.5 5.5 0 004.5 18V19"/>',
    # Две стрелки — «ответить всем», как в почтовых клиентах.
    "reply-all": '<path d="M8.5 8L4 12.5 8.5 17"/><path d="M13 8l-4.5 4.5L13 17"/>'
                 '<path d="M8.5 12.5H16a5 5 0 015 5V19"/>',
    "thread": '<rect x="3" y="4" width="14" height="10" rx="2"/>'
              '<path d="M7 17.5h11a2 2 0 002-2V8"/>',
    # Квадрат с карандашом — «написать письмо».
    "compose": '<path d="M20 12.5V19a2 2 0 01-2 2H5a2 2 0 01-2-2V6a2 2 0 012-2h6.5"/>'
               '<path d="M17.5 3.2a2 2 0 012.8 2.8L12 14.3l-3.6.9.9-3.6 8.2-8.4z"/>',
    "inbox": '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11L2 12v6a2 2 0 002 2h16a2 2 0 002-2v-6l-3.45-6.89A2 2 0 0016.76 4H7.24a2 2 0 00-1.79 1.11z"/>',
    "sent": '<path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4 20-7z"/>',
    "folder": '<path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/>',
    "clock": '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    "archive": '<rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v11a1 1 0 001 1h12a1 1 0 001-1V8"/><path d="M10 12h4"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    "calendar": '<rect x="3.5" y="5" width="17" height="15.5" rx="2"/><path d="M3.5 9.5h17M8 3.5v3M16 3.5v3"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "handset": '<rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M10.5 18.5h3"/>',
    "mic": '<rect x="9" y="2.5" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0013 0"/><path d="M12 17.5V21M8.5 21h7"/>',
    "dot": '<circle cx="12" cy="12" r="2.5" fill="currentColor" stroke="none"/>',
    "refresh": '<path d="M20.5 12a8.5 8.5 0 11-2.5-6"/><path d="M20.5 3.5v5h-5"/>',
    "chevron": '<path d="M6.5 9.5l5.5 5.5 5.5-5.5"/>',
    "trash": '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
    "more": '<circle cx="5" cy="12" r="1.6" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.6" fill="currentColor" stroke="none"/><circle cx="19" cy="12" r="1.6" fill="currentColor" stroke="none"/>',
    # A decision made at a meeting: planted, settled, not a task.
    "flag": '<path d="M5.5 21V4"/><path d="M5.5 4.5h12l-2.5 4 2.5 4h-12"/>',
}


def icon(name: str, cls: str = "") -> Markup:
    inner = _ICONS.get(name, _ICONS["dot"])
    extra = f" {cls}" if cls else ""
    return Markup(
        f'<svg class="ic{extra}" viewBox="0 0 24 24" width="1em" height="1em" '
        f'fill="none" stroke="currentColor" stroke-width="1.7" '
        f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{inner}</svg>'
    )
