"""Step previews expose only allowlisted, bounded argument details."""

import pytest


CASES = [
    ('terminal', {'command': 'git status -s'}, 'git'),
    ('terminal', {'command': 'curl -u admin:hunter2pass https://x.dev'}, 'curl'),
    ('terminal', {'command': 'mysql -u root -pHunter2xyz db'}, 'mysql'),
    ('terminal', {'command': 'export GITHUB_TOKEN=ghp_' + 'a' * 36 + ' && gh pr list'}, 'export'),
    ('terminal', {'command': 'psql postgresql://app:S3cret@db.x.co/db'}, 'psql'),
    ('web_extract', {'urls': ['https://user:pw@api.example.com/v1/x?token=abc#f']}, 'api.example.com'),
    ('browser_navigate', {'url': 'https://example.com/'}, 'example.com'),
    ('read_file', {'path': 'src/app.ts'}, 'app.ts'),
    ('web_search', {'query': 'swiftui disclosure animation'}, None),
    ('terminal', {'command': 'login hunter2 now'}, 'login'),
    ('terminal', {'command': 'TOKEN=abc ./run'}, None),
    ('terminal', {'command': '/usr/bin/git status'}, 'git'),
    ('terminal', {'command': './run.sh --secret'}, 'run.sh'),
    ('terminal', {'command': 'x' * 65}, None),
    ('execute_code', {'code': 'print(123)'}, None),
    ('process', {'command': 'python worker.py'}, 'python'),
    ('browser_navigate', {'url': 'https://hooks.slack.com/services/T000/B000/XXXXXXXXXXXXXXXX'}, 'hooks.slack.com'),
    ('browser_navigate', {'url': 'https://api.telegram.org/bot123456:ABCdefGhIJKlmnoPQRsTUVwxyz/sendMessage'}, 'api.telegram.org'),
    ('browser_navigate', {'url': 'https://user:pw@EXAMPLE.com:8443/path'}, 'example.com'),
    ('web_extract', {'urls': ['https://cdn.example.com/signed/opaqueCapability1234567890/object?sig=x']}, 'cdn.example.com'),
    ('read_file', {'path': '/tmp/opaqueCapability1234567890/data.json'}, 'data.json'),
    ('write_file', {'path': 'src/app.ts/'}, 'app.ts'),
    ('patch', {'path': '/'}, None),
    ('search_files', {'path': ''}, None),
    ('web_search', {'query': 'login hunter2 fails'}, None),
    ('session_search', {'query': 'login hunter2 fails'}, None),
    ('todo', {'items': []}, None),
    ('delegate_task', {'goal': 'Audit the iOS chat steps view for layout jumps and report'}, 'Audit the iOS chat steps view…'),
    ('delegate_task', {'goal': '  Fix\nthe build  '}, 'Fix the build'),
    ('delegate_task', {'tasks': [{'goal': 'a'}, {'goal': 'b'}, {'goal': 'c'}]}, '3 tasks'),
    ('delegate_task', {'tasks': [{'goal': 'Check the login page loads'}]}, 'Check the login page loads'),
    ('delegate_task', {'goal': 'Use key ghp_' + 'a' * 36 + ' to list'}, None),
    ('delegate_task', {'goal': 'x' * 300}, None),
    ('delegate_task', {}, None),
    ('terminal', 'not a dict', None),
]


@pytest.mark.parametrize('name,args,expected', CASES)
def test_step_preview(name, args, expected):
    from gateway.weave_step_preview import step_preview

    assert step_preview(name, args) == expected


def test_step_preview_never_contains_a_secret():
    from gateway.weave_step_preview import step_preview

    for name, args, _ in CASES:
        preview = step_preview(name, args) or ''
        for secret in ('hunter2pass', 'Hunter2xyz', 'ghp_', 'S3cret', 'pw@', 'token=abc', 'hunter2', 'XXXXXXXXXXXXXXXX', 'ABCdefGh', 'opaqueCapability', 'T000'):
            assert secret not in preview


def test_step_preview_is_bounded_and_single_line():
    from gateway.weave_step_preview import step_preview

    preview = step_preview('read_file', {'path': 'src/\n' + 'a' * 500})
    assert preview.startswith('a')
    assert len(preview) <= 120
    assert '\n' not in preview


def test_step_preview_redacts_before_capping_with_forced_url_credential_redaction(monkeypatch):
    from gateway.weave_step_preview import step_preview

    calls = []

    def redact(text, **kwargs):
        calls.append((text, kwargs))
        return "r" * 200

    monkeypatch.setattr("agent.redact.redact_sensitive_text", redact)
    assert step_preview("read_file", {"path": "src/" + "a" * 200}) == "r" * 120
    assert calls == [("a" * 200, {"force": True, "redact_url_credentials": True})]
