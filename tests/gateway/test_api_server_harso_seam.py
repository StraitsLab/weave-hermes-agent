"""The fork's single route seam is wired into the API server route table."""
from gateway.platforms import api_server, api_server_harso


def test_seam_is_called_from_the_route_table():
    src = open(api_server.__file__, encoding="utf-8").read()
    assert "_harso.merge_routes(routes, _harso._http_routes(self))" in src


def test_harso_route_replaces_native_route_with_same_method_and_path():
    native = [("GET", "/a", "native-a"), ("POST", "/api/jobs", "native-create"), ("GET", "/api/jobs", "native-list")]
    harso = [("POST", "/api/jobs", "harso-create"), ("GET", "/v1/mcp/reload", "harso-reload")]
    merged = api_server_harso.merge_routes(native, harso)
    assert merged == [("GET", "/a", "native-a"), ("GET", "/api/jobs", "native-list"),
                      ("POST", "/api/jobs", "harso-create"), ("GET", "/v1/mcp/reload", "harso-reload")]


def test_no_harso_routes_leaves_native_table_unchanged():
    native = [("GET", "/a", "x")]
    assert api_server_harso.merge_routes(native, []) == native
