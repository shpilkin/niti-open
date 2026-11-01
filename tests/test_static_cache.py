"""Окно приложения не должно показывать вчерашний CSS.

С постоянным портом адрес страницы стал одним и тем же от запуска к запуску, и WebKit
держал app.css от 19.09: календарь в установленной сборке рисовался без единого своего
стиля — дни столбиком, часы списком, встречи строчкой внизу.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from email_reader.api.app import asset_version, create_app


def test_static_files_are_revalidated_not_taken_from_cache_on_faith():
    with TestClient(create_app()) as client:
        response = client.get("/static/app.css")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers.get("etag"), "без ETag перепроверка стоила бы полного файла"


def test_the_page_asks_for_styles_by_their_fingerprint(tmp_path):
    (tmp_path / "app.css").write_text(".a{}")
    before = asset_version(tmp_path)
    (tmp_path / "app.css").write_text(".a{color:red}")
    after = asset_version(tmp_path)
    assert before != after, "новый CSS — новый адрес, старая копия его не подменит"

    import email_reader.api.app  # noqa: F401 — регистрирует глобалы шаблонов
    from email_reader.api.common import templates

    source = templates.env.loader.get_source(templates.env, "base.html")[0]
    for asset in ("htmx-1.9.12.min.js", "tokens.css", "app.css", "ui.js"):
        assert f"/static/{asset}?v={{{{ ASSET_V }}}}" in source, asset
    assert templates.env.globals["ASSET_V"]
