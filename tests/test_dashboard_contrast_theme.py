"""Regressão de contraste e do tema claro/escuro por horário.

Dois defeitos reais motivaram este arquivo:
  1. showActionBanner aplicava fundo escuro inline com texto escuro (ilegível).
  2. Não existia tema escuro, então os fundos brancos translúcidos e as cores
     claras dos badges ficavam sem par.
"""
import re
import unittest

DASHBOARD = "dashboard.html"


def _read():
    with open(DASHBOARD, encoding="utf-8") as f:
        return f.read()


def _css(html):
    return html[html.index("<style>"):html.index("</style>")]


def _root_block(css):
    start = css.index(":root {")
    return css[start: css.index("}", start)]


def _dark_block(css):
    start = css.index('html[data-theme="dark"] {')
    return css[start: css.index("\n        }", start)]


def _tokens(block):
    return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", block))


def _lum(hexcolor):
    h = hexcolor.lstrip("#")
    if len(h) == 8:
        h = h[:6]
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda v: v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _contrast(a, b):
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


class TestBannerContrast(unittest.TestCase):
    def test_banner_has_no_inline_hex(self):
        # Só o corpo de showActionBanner importa: os mesmos hex podem existir como
        # definição de token no tema escuro.
        html = _read()
        start = html.index("function showActionBanner")
        body = html[start: html.index("\n        }", start)]
        for hexcolor in ("#14532d", "#78350f", "#1e3a5f", "#22c55e", "#facc15", "#38bdf8"):
            self.assertNotIn(hexcolor, body, f"cor inline {hexcolor} voltou ao banner")

    def test_banner_uses_classes(self):
        html = _read()
        self.assertIn("banner-success", html)
        self.assertIn("banner-warn", html)
        self.assertIn("banner-info", html)

    def test_banner_pairs_meet_contrast_both_themes(self):
        css = _css(_read())
        pairs = [
            ("--success-ink", "--success-dim"),
            ("--gold-ink", "--gold-dim"),
            ("--info-ink", "--info-dim"),
            ("--series-ink", "--series-dim"),
            ("--danger-ink", "--danger-dim"),
        ]
        for name, block in (("claro", _root_block(css)), ("escuro", _dark_block(css))):
            tokens = _tokens(block)
            for fg, bg in pairs:
                with self.subTest(tema=name, par=f"{fg}/{bg}"):
                    ratio = _contrast(tokens[fg], tokens[bg])
                    self.assertGreaterEqual(ratio, 4.5, f"{name} {fg} sobre {bg} = {ratio:.2f}:1")


class TestDarkThemeTokens(unittest.TestCase):
    def test_dark_theme_defines_every_light_color_token(self):
        css = _css(_read())
        light = _tokens(_root_block(css))
        dark = _tokens(_dark_block(css))
        non_color = ("--sp-", "--fs-", "--r-", "--shadow", "--ease", "--font",
                     "--nav-h", "--btn-radius")
        color_tokens = [k for k in light if not k.startswith(non_color)]
        missing = [k for k in color_tokens if k not in dark]
        self.assertEqual(missing, [], f"tokens sem par no tema escuro: {missing}")

    def test_body_text_contrast_both_themes(self):
        css = _css(_read())
        for name, block in (("claro", _root_block(css)), ("escuro", _dark_block(css))):
            t = _tokens(block)
            for fg in ("--text-primary", "--text-muted", "--text-dim"):
                with self.subTest(tema=name, token=fg):
                    for bg in ("--bg", "--surface-1", "--surface-2"):
                        ratio = _contrast(t[fg], t[bg])
                        self.assertGreaterEqual(ratio, 4.5, f"{name} {fg} em {bg} = {ratio:.2f}:1")

    def test_primary_button_label_readable(self):
        css = _css(_read())
        for name, block in (("claro", _root_block(css)), ("escuro", _dark_block(css))):
            t = _tokens(block)
            ratio = _contrast(t["--on-primary"], t["--primary"])
            self.assertGreaterEqual(ratio, 4.5, f"{name} on-primary/primary = {ratio:.2f}:1")

    def test_no_opaque_white_surfaces_left(self):
        # Fundos brancos fixos viram texto invisível no tema escuro.
        css = _css(_read())
        offenders = re.findall(r"background:\s*rgba\(255,\s*255,\s*255,\s*0?\.9[0-9]*\)", css)
        self.assertEqual(offenders, [], "superfície branca translúcida fixa encontrada")


class TestTimeTheme(unittest.TestCase):
    def test_theme_script_runs_in_head(self):
        html = _read()
        head = html[: html.index("</head>")]
        self.assertIn("America/Sao_Paulo", head)
        self.assertIn("data-theme", head)
        # Precisa vir antes do <style> para não piscar na cor errada.
        self.assertLess(head.index("America/Sao_Paulo"), html.index("<style>"))

    def test_day_window_is_6_to_18(self):
        html = _read()
        head = html[: html.index("</head>")]
        self.assertIn("DAY_START = 6", head)
        self.assertIn("DAY_END = 18", head)

    def test_theme_reapplies_over_time(self):
        html = _read()
        head = html[: html.index("</head>")]
        self.assertIn("setInterval", head)
        self.assertIn("visibilitychange", head)

    def test_theme_color_meta_matches_light_theme(self):
        html = _read()
        meta = re.search(r'<meta name="theme-color" content="([^"]+)"', html)
        self.assertIsNotNone(meta)
        self.assertEqual(meta.group(1).lower(), _tokens(_root_block(_css(html)))["--bg"].lower())


class TestTouchablePadding(unittest.TestCase):
    def test_cancel_and_delete_have_horizontal_padding(self):
        css = _css(_read())
        start = css.index(".btn-cancel-vod,")
        block = css[start: css.index("}", start)]
        self.assertIn("padding: 0 var(--sp-4)", block)


if __name__ == "__main__":
    unittest.main()
