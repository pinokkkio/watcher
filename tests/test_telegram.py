import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from moex_watcher.config import load_config
from moex_watcher.notifiers import TelegramNotifier, build_proxy_url, mask_proxy

USER, PASSWORD = "bot user", "p@ss:w/rd#1"


class ProxyHandler(BaseHTTPRequestHandler):
    """Мини-прокси с Basic-авторизацией, отвечающий вместо Telegram."""

    seen: list = []

    def log_message(self, *a):
        pass

    def _authorized(self):
        expected = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        return self.headers.get("Proxy-Authorization") == expected

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.seen.append(("POST", self.path, self.headers.get("Proxy-Authorization"), body))
        if not self._authorized():
            self.send_response(407)
            self.send_header("Proxy-Authenticate", 'Basic realm="p"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        out = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_CONNECT(self):
        self.seen.append(("CONNECT", self.path, self.headers.get("Proxy-Authorization"), b""))
        self.send_response(407)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def proxy():
    ProxyHandler.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def test_build_proxy_url_escapes_credentials():
    url = build_proxy_url("https://proxy.example.com:3128", USER, PASSWORD)
    assert url == "https://bot%20user:p%40ss%3Aw%2Frd%231@proxy.example.com:3128"
    assert build_proxy_url("proxy.example.com:8443").startswith("https://")
    assert mask_proxy(url) == "https://bot%20user:***@proxy.example.com:3128"
    with pytest.raises(ValueError):
        build_proxy_url("https://proxy.example.com")  # нет порта


def test_messages_go_through_authenticated_proxy(proxy, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://wrong:1")  # должен игнорироваться
    host, port = proxy.server_address
    tg = TelegramNotifier("123:TOKEN", "42", proxy=f"http://{host}:{port}", proxy_user=USER,
                          proxy_password=PASSWORD, api_base="http://api.telegram.test")
    assert tg.check("привет")
    method, path, auth, body = ProxyHandler.seen[-1]
    assert path == "http://api.telegram.test/bot123:TOKEN/sendMessage"
    assert auth == "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    assert json.loads(body) == {"chat_id": "42", "text": "привет", "parse_mode": "HTML",
                                "disable_web_page_preview": True}


def test_wrong_password_is_reported(proxy, caplog):
    host, port = proxy.server_address
    tg = TelegramNotifier("t", "1", proxy=f"http://{host}:{port}", proxy_user=USER, proxy_password="bad",
                          api_base="http://api.telegram.test")
    assert not tg.check("x")
    assert "407" in caplog.text and "bad" not in caplog.text


def test_https_api_uses_connect_with_credentials(proxy, monkeypatch):
    monkeypatch.setattr("moex_watcher.notifiers.time.sleep", lambda s: None)
    host, port = proxy.server_address
    tg = TelegramNotifier("t", "1", proxy=f"http://{host}:{port}", proxy_user=USER, proxy_password=PASSWORD,
                          retries=1)
    assert not tg.check("x")  # наш тестовый прокси туннель не открывает
    method, path, auth, _ = ProxyHandler.seen[-1]
    assert (method, path) == ("CONNECT", "api.telegram.org:443")
    assert auth == "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
    monkeypatch.setenv("TELEGRAM_PROXY", "https://p:443")
    monkeypatch.setenv("TELEGRAM_PROXY_USER", "u")
    monkeypatch.setenv("TELEGRAM_PROXY_PASSWORD", "pw")
    monkeypatch.setenv("WATCH_MARKETS", "shares,futures")
    cfg = load_config(None)
    assert (cfg.telegram_token, cfg.telegram_chat_id, cfg.telegram_proxy) == ("tok", "-100", "https://p:443")
    assert (cfg.telegram_proxy_user, cfg.telegram_proxy_password) == ("u", "pw")
    assert [m.key for m in cfg.markets] == ["shares", "futures"]


def test_tls_proxy_is_verified_and_authenticated(tmp_path, monkeypatch):
    """https://-прокси: TLS до прокси проверяется, CONNECT к api.telegram.org идёт с Basic-авторизацией."""
    import ssl
    import subprocess

    monkeypatch.setattr("moex_watcher.notifiers.time.sleep", lambda s: None)
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", crt,
                    "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
                   check=True, capture_output=True)
    ProxyHandler.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        proxy = f"https://localhost:{srv.server_address[1]}"
        # сертификат прокси не доверенный -> соединение не устанавливается, креды не уходят
        tg = TelegramNotifier("t", "1", proxy=proxy, proxy_user=USER, proxy_password=PASSWORD, retries=1)
        assert not tg.check("x")
        assert ProxyHandler.seen == []
        # с CA прокси -> TLS поднят, CONNECT с авторизацией
        tg = TelegramNotifier("t", "1", proxy=proxy, proxy_user=USER, proxy_password=PASSWORD, retries=1,
                              proxy_ca=str(crt))
        tg.check("x")
        method, path, auth, _ = ProxyHandler.seen[-1]
        assert (method, path) == ("CONNECT", "api.telegram.org:443")
        assert auth == "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    finally:
        srv.shutdown()
