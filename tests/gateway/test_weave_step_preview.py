"""Step previews expose only allowlisted, bounded argument details."""

import pytest


CASES = [
    ('terminal', {'command': 'git status -s'}, 'git status'),
    ('terminal', {'command': 'curl -u admin:hunter2pass https://x.dev'}, 'curl'),
    ('terminal', {'command': 'mysql -u root -pHunter2xyz db'}, 'mysql'),
    ('terminal', {'command': 'export GITHUB_TOKEN=ghp_' + 'a' * 36 + ' && gh pr list'}, 'export'),
    ('terminal', {'command': 'psql postgresql://app:S3cret@db.x.co/db'}, 'psql'),
    ('web_extract', {'urls': ['https://user:pw@api.example.com/v1/x?token=abc#f']}, 'api.example.com/v1/x'),
    ('read_file', {'path': 'src/app.ts'}, 'src/app.ts'),
    ('web_search', {'query': 'swiftui disclosure animation'}, 'swiftui disclosure animation'),
    ('todo', {'items': []}, None),
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
        for secret in ('hunter2pass', 'Hunter2xyz', 'ghp_', 'S3cret', 'pw@', 'token=abc'):
            assert secret not in preview


def test_step_preview_is_bounded_and_single_line():
    from gateway.weave_step_preview import step_preview

    preview = step_preview('read_file', {'path': 'src/\n' + 'a' * 500})
    assert preview.startswith('src/ a')
    assert len(preview) <= 120
    assert '\n' not in preview
