"""The frozen-backend entrypoint (src/desktop_entry.py) contract.

Guards the small surface Electron depends on: main() exists, reads PORT from the
env, binds loopback only, and drives the real app object. See
doc/electron-desktop-app-design.md (Phase 2).
"""

import src.desktop_entry as de


def test_main_reads_port_and_binds_loopback(monkeypatch):
    monkeypatch.setenv("PORT", "51999")
    captured = {}

    def fake_run(app, host, port, **kwargs):
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port

    # Avoid actually importing/booting the app or opening a socket.
    monkeypatch.setattr("uvicorn.run", fake_run)

    import src.main as srcmain

    monkeypatch.setattr(srcmain, "app", "SENTINEL_APP", raising=False)

    de.main()

    assert captured["port"] == 51999
    assert captured["host"] == "127.0.0.1"  # loopback only, never 0.0.0.0
    assert captured["app"] == "SENTINEL_APP"


def test_main_defaults_port_when_unset(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    captured = {}
    monkeypatch.setattr("uvicorn.run", lambda app, host, port, **kw: captured.update(port=port))
    de.main()
    assert captured["port"] == 8000
