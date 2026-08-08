import os
from pathlib import Path

from agent_monitor import paths
from agent_monitor.config import load_pad_config


def test_runtime_paths_use_xdg_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert paths.socket_path() == tmp_path / "agent-monitor" / "daemon.sock"
    assert paths.state_path() == tmp_path / "agent-monitor" / "state.json"
    assert paths.socket_path().parent.is_dir()


def test_missing_config_disables_pad(tmp_path):
    assert load_pad_config(tmp_path / "nope.toml") is None


def test_disabled_config_disables_pad(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[pad]\nenabled = false\nhost = "deepdeck.local"\n')
    assert load_pad_config(p) is None


def test_config_parses_pad_section(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[pad]\nenabled = true\nhost = "10.0.0.5"\napi_key = "abc"\n')
    cfg = load_pad_config(p)
    assert (cfg.host, cfg.api_key, cfg.port) == ("10.0.0.5", "abc", 6053)


def test_runtime_dir_falls_back_to_tmp(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    expected = Path(f"/tmp/agent-monitor-{os.getuid()}") / "agent-monitor"
    assert paths.socket_path() == expected / "daemon.sock"
    assert expected.is_dir()


def test_config_path_respects_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert paths.config_path() == tmp_path / "agent-monitor" / "config.toml"


def test_malformed_numeric_values_disable_pad(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[pad]\nenabled = true\nhost = "10.0.0.5"\nport = "abc"\n')
    assert load_pad_config(p) is None


def test_reconnect_delay_has_a_floor(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[pad]\nenabled = true\nhost = "h"\nreconnect_delay = 0\n')
    assert load_pad_config(p).reconnect_delay == 0.5
