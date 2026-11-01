from __future__ import annotations

import uuid

from email_reader import htmlview, signatures


def test_sanitizer_keeps_style_strips_scripts():
    dirty = ('<span style="color:#e11;font-weight:bold">Глеб</span>'
             '<script>alert(1)</script>'
             '<a href="https://x.ru" onclick="evil()">сайт</a>')
    clean = htmlview.sanitize_signature_html(dirty)
    assert "color:#e11" in clean           # inline style preserved
    assert "<span" in clean
    assert "script" not in clean.lower()    # scripts stripped
    assert "onclick" not in clean.lower()   # event handlers stripped
    assert "https://x.ru" in clean          # link kept


def test_signature_to_text():
    assert htmlview.signature_to_text("<div>Глеб<br>+7 900</div>") == "Глеб\n+7 900"
    assert htmlview.signature_to_text("") == ""


async def test_set_signature_sanitizes(db):
    acc = uuid.uuid4()
    async with db() as s:
        await signatures.set_signature(s, acc, '<b style="color:red">Имя</b><script>x</script>')
        await s.commit()
        stored = await signatures.get_signature(s, acc)
        assert "color:red" in stored
        assert "script" not in stored.lower()
