"""Regressão da aposentadoria da aba "Ao Vivo" e da página legada /tv.

O menu do app PWA passa a ter Filmes / Séries / Salvos. A grade completa do
provedor continua em /tv, mas só o link direto no navegador a abre: a PWA
instalada é devolvida para /.
"""
import re
import threading
import unittest
import urllib.request


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
        return r.status, r.read().decode("utf-8", "replace")


class TestLegacyLivePage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from server import ThreadedHTTPServer, RequestHandler

        cls.httpd = ThreadedHTTPServer(("127.0.0.1", 0), RequestHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_root_does_not_mark_legacy(self):
        status, html = _get(self.port, "/")
        self.assertEqual(status, 200)
        self.assertNotIn("window.__LEGACY_LIVE__ = true", html)

    def test_tv_serves_legacy_flag(self):
        status, html = _get(self.port, "/tv")
        self.assertEqual(status, 200)
        self.assertIn("window.__LEGACY_LIVE__ = true", html)

    def test_legacy_flag_runs_before_main_script(self):
        # A flag precisa chegar antes do script principal, que chama init() no fim do body.
        _, html = _get(self.port, "/tv")
        pos_flag = html.index("window.__LEGACY_LIVE__ = true")
        pos_snippet_end = html.index("</head>")
        pos_main = html.index("function isLegacyLive")
        self.assertLess(pos_flag, pos_snippet_end)
        self.assertLess(pos_flag, pos_main)

    def test_head_snippet_has_pwa_bailout(self):
        _, html = _get(self.port, "/tv")
        head = html[: html.index("</head>")]
        self.assertIn("display-mode: standalone", head)
        self.assertIn("location.replace('/')", head)

    def test_head_snippet_is_self_contained(self):
        # Precisa funcionar mesmo se o script principal falhar.
        from server import LEGACY_LIVE_SNIPPET

        self.assertNotIn("isStandalonePWA", LEGACY_LIVE_SNIPPET)
        self.assertNotIn("isLegacyLive", LEGACY_LIVE_SNIPPET)
        self.assertIn("location.replace('/')", LEGACY_LIVE_SNIPPET)

    def test_served_manifest_shortcuts_match_deep_link_aliases(self):
        # O manifest servido é gerado em server.py; o manifest.json em disco é ignorado.
        import json

        _, body = _get(self.port, "/manifest.json")
        manifest = json.loads(body)
        with open("dashboard.html", encoding="utf-8") as f:
            html = f.read()
        block = html[html.index("const aliases = {"):]
        block = block[: block.index("};")]
        aliases = dict(re.findall(r"([\w'áéíóúãõç-]+):\s*'(\w+)'", block))
        self.assertTrue(manifest["shortcuts"], "manifest sem atalhos")
        for shortcut in manifest["shortcuts"]:
            m = re.search(r"[?&]tab=([\w-]+)", shortcut.get("url", ""))
            if m:
                self.assertIn(m.group(1), aliases, "atalho aponta para aba desconhecida: %s" % shortcut["name"])
            self.assertNotEqual(shortcut.get("name"), "TV ao Vivo", "atalho ainda promete TV ao Vivo")

    def test_injection_is_idempotent_per_request(self):
        # Duas requisições não acumulam cópias do snippet.
        _, first = _get(self.port, "/tv")
        _, second = _get(self.port, "/tv")
        self.assertEqual(
            first.count("window.__LEGACY_LIVE__ = true"),
            second.count("window.__LEGACY_LIVE__ = true"),
        )


class TestNavHidingIsCssOnly(unittest.TestCase):
    """A aba "Ao Vivo" não pode depender de JS para sumir: senão pisca 4 abas."""

    def test_css_hides_live_tab_unless_marked_legacy(self):
        with open("dashboard.html", encoding="utf-8") as f:
            html = f.read()
        style = html[html.index("<style>"):html.index("</style>")]
        self.assertIn("html:not(.is-legacy-live) #nav-live", style)

    def test_head_snippet_marks_html_element(self):
        from server import LEGACY_LIVE_SNIPPET

        self.assertIn("classList.add('is-legacy-live')", LEGACY_LIVE_SNIPPET)

    def test_init_does_not_hide_nav_with_inline_style(self):
        with open("dashboard.html", encoding="utf-8") as f:
            html = f.read()
        self.assertNotIn("navLiveBtn.style.display = isLegacyLive()", html)


class TestLegacyLiveAssets(unittest.TestCase):
    def test_service_worker_never_caches_tv(self):
        with open("sw.js", encoding="utf-8") as f:
            sw = f.read()
        self.assertIn("url.pathname === '/tv'", sw)

    def test_embedded_service_worker_has_bypass(self):
        with open("server.py", encoding="utf-8") as f:
            src = f.read()
        start = src.index("def send_service_worker")
        end = src.index("def ", start + 10)
        self.assertIn("url.pathname === '/tv'", src[start:end])

    def test_cache_version_is_consistent(self):
        with open("sw.js", encoding="utf-8") as f:
            sw_version = re.search(r"CACHE_NAME = '([^']+)'", f.read()).group(1)
        with open("dashboard.html", encoding="utf-8") as f:
            self.assertIn(sw_version, f.read())
        with open("server.py", encoding="utf-8") as f:
            src = f.read()
        for m in re.finditer(r"CACHE_NAME = '([^']+)'", src):
            self.assertEqual(m.group(1), sw_version)
        for m in re.finditer(r"caches\.delete|k !== '([^']+)'", src):
            if m.group(1):
                self.assertEqual(m.group(1), sw_version)



if __name__ == "__main__":
    unittest.main()
