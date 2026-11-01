"""Маршруты списка писем: кто какой путь обслуживает."""

from __future__ import annotations

from email_reader.api.app import app


def _routes() -> list[tuple[str, str]]:
    return [(r.path, r.name) for r in app.routes if hasattr(r, "path")]


def test_the_boxes_have_a_list_of_their_own():
    paths = dict((p, n) for p, n in _routes())
    assert paths["/mail/{box}/list"] == "mail_list"
    assert paths["/mail/account/{account_id}/{box}/list"] == "account_mail_list"


def test_folders_keep_their_own_lists():
    """`/mail/account/{id}/{box}/list` совпадает по форме с путями отдельных папок
    (черновики и другие). Стой он выше — их опрос отдавал бы список входящих, и такие
    экраны замерли бы на том, что было при загрузке."""
    order = [p for p, _ in _routes() if p.endswith("/list")]
    generic = order.index("/mail/account/{account_id}/{box}/list")
    assert order.index("/mail/account/{account_id}/drafts/list") < generic
