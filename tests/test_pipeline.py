from __future__ import annotations

from email_reader.pipeline.cleaning import clean_body, html_to_text
from email_reader.pipeline.triage import triage


def test_clean_strips_quote_header_and_below():
    text = "Привет, согласен.\nВ 10:00 Иван написал:\n> старый текст\n> ещё"
    assert clean_body(text, None) == "Привет, согласен."


def test_clean_strips_signature():
    text = "Сделаю до пятницы.\n--\nИван Петров\nCEO"
    assert clean_body(text, None) == "Сделаю до пятницы."


def test_clean_strips_quoted_lines():
    text = "ответ\n> цитата 1\n> цитата 2\nещё ответ"
    assert clean_body(text, None) == "ответ\nещё ответ"


def test_clean_html_fallback():
    html = "<html><body><p>Привет</p><script>x()</script></body></html>"
    assert "Привет" in clean_body(None, html)
    assert "x()" not in html_to_text(html)


def test_html_inline_tags_do_not_break_the_line():
    r"""Outlook splits one sentence into <span lang="EN-US"> pieces; the old extraction
    put each on its own line («ivanov\n@\ncorpmail\n.\nru»)."""
    html = (
        '<span>e-mail:</span> <span lang="EN-US">ivanov</span>@'
        '<span lang="EN-US">corpmail</span>.<span lang="EN-US">ru</span>'
    )
    assert html_to_text(html) == "e-mail: ivanov@corpmail.ru"


def test_html_blocks_break_the_line_once_each():
    assert html_to_text("<p>раз</p><p>два</p>") == "раз\n\nдва"
    assert html_to_text("<div>раз</div>\n   <div>два</div>") == "раз\nдва"
    assert (
        html_to_text("<table><tr><td>Сумма</td><td>3129</td></tr></table>")
        == "Сумма\n3129"
    )
    assert html_to_text("a<br>b<br><br>c") == "a\nb\n\nc"


def test_html_drops_conditional_comments_and_nbsp():
    html = "<!--[if mso]><i>только для Outlook</i><![endif]--><div>Привет,&nbsp;Глеб</div>"
    assert html_to_text(html) == "Привет, Глеб"


def test_triage_list_unsubscribe():
    low, reason = triage({"list-unsubscribe": "<mailto:u@x>"}, "news@shop.com")
    assert low and "list" in reason.lower()


def test_triage_precedence_bulk():
    low, _ = triage({"precedence": "bulk"}, "a@b.c")
    assert low


def test_triage_noreply_sender():
    low, reason = triage({}, "no-reply@service.com")
    assert low and "bulk sender" in reason


def test_triage_normal_human_mail():
    low, reason = triage({}, "ivan@acme.ru")
    assert not low and reason is None
