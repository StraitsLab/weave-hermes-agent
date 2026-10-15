import json
import pytest
from agent.redact import *

class TestVaultValueEncodedForms:
    """V9: a filled vault value reaches browser output escaped or encoded, not only as raw bytes. Every form
    observed against a real Chrome + agent-browser (tests/tools/test_vault_adversarial_eval.py) is scrubbed."""

    CANARY = 'Pa ss"w\\o+rd&\u00fc/Zq7f3eK9x<b>'

    @pytest.fixture(autouse=True)
    def _registered(self):
        from agent import redact

        redact.register_vault_redaction_value(self.CANARY)
        yield
        redact.clear_vault_redaction_values()

    @pytest.mark.parametrize("encode", [
        lambda v: v,
        lambda v: __import__("json").dumps(v)[1:-1],                                  # snapshot line / tool JSON
        lambda v: __import__("json").dumps(__import__("json").dumps(v))[1:-1],        # nested JSON
        lambda v: __import__("json").dumps(v, ensure_ascii=True)[1:-1],               # \\u00fc
        lambda v: __import__("urllib.parse").parse.quote(v, safe=""),                 # encodeURIComponent
        lambda v: __import__("re").sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(),
                                       __import__("urllib.parse").parse.quote(v, safe="")),  # lowercase hex
        lambda v: __import__("urllib.parse").parse.quote_plus(v),                     # form GET, utf-8 page
        lambda v: __import__("urllib.parse").parse.quote_plus(v, encoding="cp1252"),  # form GET, no charset
        lambda v: __import__("html").escape(v, quote=True),                           # serialized DOM
        lambda v: "".join(f"\\u{ord(c):04x}" for c in v),                           # every char \\u-escaped
        lambda v: __import__("re").sub(r"\\u[0-9a-f]{4}", lambda m: m.group(0).upper().replace("U", "u"),
                                       __import__("json").dumps(v, ensure_ascii=True)[1:-1]),  # \\u00FC
        lambda v: __import__("json").dumps(v)[1:-1].replace("/", "\\/"),           # JSON's optional \\/ escape
        lambda v: str(KeyError(v)),                                                   # Python repr (KeyError, %r)
        ascii,                                                                        # repr with \\xfc
    ], ids=["raw", "json", "json2", "json-ascii", "uri", "uri-lower", "form", "form-1252", "html", "u-all", "u-upper",
            "json-solidus", "repr", "ascii"])
    def test_every_encoding_of_a_registered_value_is_scrubbed(self, encode):
        from agent.redact import redact_registered_vault_values

        out = redact_registered_vault_values(f"page said: {encode(self.CANARY)} (end)")
        assert "Zq7f3eK9x" not in out and "«redacted-vault-secret»" in out

    def test_text_that_only_shares_characters_with_the_value_is_untouched(self):
        from agent.redact import redact_registered_vault_values

        text = "STATUS Pa ss other & Zq7f3eK9 %20 \\\" plain"
        assert redact_registered_vault_values(text) == text

    def test_whole_token_values_keep_exact_token_matching(self):
        from agent import redact

        redact.register_vault_redaction_value("US", whole_token=True)
        assert redact.redact_registered_vault_values("STATUS US") == "STATUS «redacted-vault-secret»"


_TIMED_SCRUB = """
import json, sys, time
from agent import redact
values, text = json.load(sys.stdin)
for v in values:
    redact.register_vault_redaction_value(v)
start = time.monotonic()
redact.redact_registered_vault_values(text)
print(time.monotonic() - start)
"""


class TestVaultValueScrubIsBoundedTime:
    """F2 (#58 r1): matching cost must not depend on the secret's structure. Each case runs in a child process
    under a hard timeout, because a backtracking matcher holds the GIL and cannot be interrupted in-process."""

    @pytest.mark.parametrize("values, text", [
        (["\\X"], "\\" * 100_000 + "Y"),                                   # reviewer dos.log row 1
        (["\\\\X"], "\\" * 1_000 + "Y"),                                 # reviewer dos.log row 2
        (["abc" + "\\" * 8 + "Z"], "abc" + "\\" * 40 + "Y"),             # reviewer dos.log row 3
        (["abc" + "\\" * 8 + "Z"], "abc" + "\\" * 1_000 + "Y"),
        (['a\\"\\"\\"b'], '\\"' * 50_000 + "c"),                     # mixed backslash and quote
        (["aaaa/aaaa/aaaa/X"], "aaaa/" * 20_000 + "Y"),                       # repeated prefix
        ([f"p{i}\\\\\\x/y" for i in range(8)], ("\\" * 40 + "/") * 2_500),  # 8 registrations, 100KB
    ], ids=["1bs-100k", "2bs-1k", "8bs-40", "8bs-1k", "mixed", "prefix", "8regs-100k"])
    def test_near_miss_output_is_scrubbed_in_bounded_time(self, values, text):
        import subprocess
        import sys

        # The payload goes on stdin: Linux rejects a single argv string over 128KB (E2BIG) before the child starts.
        proc = subprocess.run([sys.executable, "-c", _TIMED_SCRUB], check=False, input=json.dumps([values, text]),
                              capture_output=True, text=True, timeout=10)
        assert proc.returncode == 0, proc.stderr
        assert float(proc.stdout) < 0.5
