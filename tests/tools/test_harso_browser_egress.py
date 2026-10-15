"""Browser Use must redact every emitted path, while attaching the real image."""
import json
import pytest
from tests.tools.test_browser_use_cli import _fake_cli, _fake_managed_chromium, _fake_supervisor_registry, _clean_env
from tools import browser_use_cli as bu_cli

class TestVaultEgressRedaction:
    """Ported from upstream a48debd368, plus the multimodal envelope path."""

    def test_exec_redacts_registered_vault_secret_from_stdout_and_stderr(self, tmp_path, monkeypatch):
        """A browser_exec page read must not return a vault-filled value to model history."""
        from agent import redact

        secret = "vault-filled-password-112693"
        cli = _fake_cli(tmp_path, f'cat > /dev/null\necho "stdout={secret}"\necho "stderr={secret}" >&2\n')
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: [cli])
        redact.register_vault_redaction_value(secret)
        try:
            result = json.loads(bu_cli.browser_exec("print(1)"))
        finally:
            redact.clear_vault_redaction_values()

        serialized = json.dumps(result, ensure_ascii=False)
        assert secret not in serialized
        assert serialized.count("«redacted-vault-secret»") == 2

    def test_exec_redacts_vault_secret_inside_the_multimodal_envelope(self, tmp_path, monkeypatch):
        """The screenshot path still attaches (raw stdout is used only to find it) but no text part leaks."""
        from agent import redact

        shot = tmp_path / "shot.png"
        shot.write_bytes(b"\x89PNG fake")
        secret = "vault-filled-password-551207"
        cli = _fake_cli(tmp_path, f'cat > /dev/null\necho "{shot}"\necho "value={secret}"\necho "{secret}" >&2\nexit 1\n')
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: [cli])
        monkeypatch.setattr("tools.vision_tools._should_use_native_vision_fast_path", lambda: True)
        monkeypatch.setattr("tools.vision_tools._resize_image_for_vision", lambda p, **kw: "data:image/png;base64,QUJD")
        redact.register_vault_redaction_value(secret)
        try:
            result = bu_cli.browser_exec("print(capture_screenshot())")
        finally:
            redact.clear_vault_redaction_values()

        assert isinstance(result, dict) and result["_multimodal"] is True
        assert result["meta"]["screenshot_path"] == str(shot)
        assert secret not in json.dumps(result, ensure_ascii=False)
        assert "«redacted-vault-secret»" in result["text_summary"]

    @pytest.mark.parametrize("native", [False, True])
    def test_vault_secret_in_the_screenshot_filename_never_leaves(self, tmp_path, monkeypatch, native):
        """Fork fix: screenshot_path is read from RAW stdout. A registered value inside the filename must be
        scrubbed from every emitted copy (JSON, multimodal text/meta), while the real file is still attached."""
        from agent import redact

        secret = "vaultcanaryinfilename803"
        shot = tmp_path / f"{secret}.png"
        loaded = []
        # The child writes the image itself, so the freshness check never races a loaded host.
        cli = _fake_cli(tmp_path, f'cat > /dev/null\nprintf "PNG" > "{shot}"\necho "{shot}"\n')
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: [cli])
        monkeypatch.setattr("tools.vision_tools._should_use_native_vision_fast_path", lambda: native)
        monkeypatch.setattr("tools.vision_tools._resize_image_for_vision",
                            lambda p, **kw: loaded.append(str(p)) or "data:image/png;base64,QUJD")
        redact.register_vault_redaction_value(secret)
        try:
            raw = bu_cli.browser_exec("print(capture_screenshot())")
        finally:
            redact.clear_vault_redaction_values()

        result = raw if native else json.loads(raw)
        assert secret not in json.dumps(result, ensure_ascii=False)
        if native:
            assert result["_multimodal"] is True and loaded == [str(shot)]  # the real image is still attached
            assert "«redacted-vault-secret»" in result["meta"]["screenshot_path"]
            assert "«redacted-vault-secret»" in result["text_summary"]
        else:
            assert result["screenshot_path"] == str(tmp_path / "«redacted-vault-secret».png")
