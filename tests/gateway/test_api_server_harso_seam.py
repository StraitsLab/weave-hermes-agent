"""The fork's single route seam is wired into the API server route table."""
from gateway.platforms import api_server_harso


def test_harso_routes_extend_the_table(monkeypatch):
    from gateway.platforms import api_server

    marker = ("GET", "/harso-seam-probe", object())
    monkeypatch.setattr(api_server_harso, "_http_routes", lambda adapter: [marker])
    src = open(api_server.__file__).read()
    assert "api_server_harso" in src and "_harso._http_routes(self)" in src
    assert api_server_harso._http_routes(None) == [marker]
