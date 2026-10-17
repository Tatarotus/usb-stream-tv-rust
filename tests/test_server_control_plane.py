import unittest
import time
import json
import urllib.parse
from server import CommandBus, CommandItem, resolve_vod_stream_url, HUB, dispatch_device_cmd

class TestCommandBus(unittest.TestCase):
    def setUp(self):
        self.bus = CommandBus()
        self.bus.seq = 0

    def test_submit_and_poll_v2(self):
        item = self.bus.submit("echo hello")
        self.assertEqual(item.id, 1)
        self.assertEqual(item.cmd, "echo hello")

        # Polling em modo v2
        res = self.bus.poll(wait_sec=0.1, nocmd=False, is_v2=True)
        self.assertEqual(res, "1|echo hello")

        # Não deve entregar comando duplicado imediatamente
        res2 = self.bus.poll(wait_sec=0.1, nocmd=False, is_v2=True)
        self.assertIsNone(res2)

    def test_poll_nocmd_suppression(self):
        self.bus.submit("reboot")
        # Se tablet está ocupado (nocmd=True), não deve entregar
        res = self.bus.poll(wait_sec=0.1, nocmd=True, is_v2=True)
        self.assertIsNone(res)

        # Quando desocupado, entrega
        res = self.bus.poll(wait_sec=0.1, nocmd=False, is_v2=True)
        self.assertEqual(res, "1|reboot")

    def test_legacy_poll_and_ack(self):
        item = self.bus.submit("ls -la")
        # Polling legado v1 (sem v=2)
        res = self.bus.poll(wait_sec=0.1, nocmd=False, is_v2=False)
        self.assertEqual(res, "ls -la")

        # ACK legado
        acked = self.bus.ack_legacy("total 42")
        self.assertTrue(acked)
        self.assertTrue(item.acked)
        self.assertEqual(item.output, "total 42")

    def test_ack_unblocks_event(self):
        item = self.bus.submit("sleep 1")
        res = self.bus.poll(wait_sec=0.1, nocmd=False, is_v2=True)
        self.assertEqual(res, "1|sleep 1")

        self.assertFalse(item.event.is_set())
        acked = self.bus.ack(1, rc=0, output="done")
        self.assertTrue(acked)
        self.assertTrue(item.event.is_set())
        self.assertEqual(item.output, "done")
        self.assertEqual(item.rc, 0)

    def test_dedupe_coalescing(self):
        item1 = self.bus.submit("switch_live 1", dedupe_key="live")
        item2 = self.bus.submit("switch_live 2", dedupe_key="live")

        # O segundo substitui o primeiro na fila
        self.assertEqual(item1.id, item2.id)
        self.assertEqual(len(self.bus.queue), 1)

        res = self.bus.poll(wait_sec=0.1, nocmd=False, is_v2=True)
        self.assertEqual(res, f"{item1.id}|switch_live 2")

    def test_redelivery_after_timeout(self):
        item = self.bus.submit("test_redelivery")
        res1 = self.bus.poll(wait_sec=0.1, nocmd=False, is_v2=True)
        self.assertEqual(res1, "1|test_redelivery")

        # Simula passagem de 9 segundos sem ACK
        item.delivered_at = time.time() - 9.0

        res2 = self.bus.poll(wait_sec=0.1, nocmd=False, is_v2=True)
        self.assertEqual(res2, "1|test_redelivery")
        self.assertEqual(item.delivery_count, 2)


class TestVodUrlSanitization(unittest.TestCase):
    def test_no_port_80(self):
        url = "http://example.com/movie.mp4?token=abc"
        self.assertEqual(resolve_vod_stream_url(url), url)

    def test_strip_port_80_netloc(self):
        loc = "http://cdn.stream.com:80/hls/movie.mp4?tag=test:80/foo"
        p = urllib.parse.urlsplit(loc)
        self.assertEqual(p.port, 80)
        clean_netloc = p.netloc.replace(":80", "")
        clean_loc = urllib.parse.urlunsplit((p.scheme, clean_netloc, p.path, p.query, p.fragment))
        self.assertEqual(clean_loc, "http://cdn.stream.com/hls/movie.mp4?tag=test:80/foo")

class TestHttpControlPlaneEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import threading
        from server import ThreadedHTTPServer, RequestHandler
        cls.httpd = ThreadedHTTPServer(('127.0.0.1', 0), RequestHandler)
        cls.port = cls.httpd.server_address[1]
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_head_does_not_drain_queue(self):
        HUB.bus.submit("test_head_command")
        # Faz HEAD request para /api/tablet_cmd
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/tablet_cmd?v=2", method="HEAD")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            self.assertEqual(resp.status, 204)

        # O comando AINDA deve estar na fila e ser entregue pelo GET
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/tablet_cmd?v=2", timeout=2.0) as resp:
            self.assertEqual(resp.status, 200)
            data = resp.read().decode("utf-8")
            self.assertTrue(data.endswith("|test_head_command"))

    def test_vod_blank_value_clears_vod_state(self):
        import urllib.request
        HUB.tablet_vod_mode = "vod_123"
        req = f"http://127.0.0.1:{self.port}/api/tablet_cmd?v=2&vod="
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            self.assertEqual(resp.status, 200)
        self.assertEqual(HUB.tablet_vod_mode, "")

    def test_vod_none_clears_vod_state(self):
        import urllib.request
        HUB.tablet_vod_mode = "vod_456"
        req = f"http://127.0.0.1:{self.port}/api/tablet_cmd?v=2&vod=none"
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            self.assertEqual(resp.status, 200)
        self.assertEqual(HUB.tablet_vod_mode, "")

    def test_ack_endpoint(self):
        import urllib.request
        item = HUB.bus.submit("command_to_ack")
        cmd_id = item.id
        HUB.bus.poll(is_v2=True)

        post_data = b"execution stdout output"
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/tablet_ack?id={cmd_id}&rc=0",
            data=post_data,
            headers={"Content-Type": "text/plain"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            self.assertEqual(resp.status, 200)
            body = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(body.get("success"))
            self.assertTrue(body.get("acked"))

        self.assertTrue(item.acked)
        self.assertEqual(item.output, "execution stdout output")

if __name__ == "__main__":
    unittest.main()
