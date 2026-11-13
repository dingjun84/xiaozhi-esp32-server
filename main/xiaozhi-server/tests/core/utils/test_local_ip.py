"""Configured advertised IP must bypass route probing, including VPN routes."""

import unittest
from unittest.mock import patch

from core.utils.util import get_local_ip, get_vision_url


class LocalIPTests(unittest.TestCase):
    def test_configured_ip_bypasses_socket(self):
        with patch("core.utils.util.socket.socket") as factory:
            for ip in ("127.0.0.1", "192.168.1.25", " 192.168.1.25 "):
                with self.subTest(ip=ip):
                    self.assertEqual(get_local_ip({"server": {"local_ip": ip}}), ip.strip())
            factory.assert_not_called()

    def test_empty_config_preserves_auto_detection_and_closes_socket(self):
        with patch("core.utils.util.socket.socket") as factory:
            sock = factory.return_value.__enter__.return_value
            sock.getsockname.return_value = ("192.168.1.10", 1234)
            for config in (None, {}, {"server": {}}, {"server": {"local_ip": " "}},
                           {"server": {"local_ip": None}}):
                with self.subTest(config=config):
                    self.assertEqual(get_local_ip(config), "192.168.1.10")
            sock.connect.assert_called_with(("8.8.8.8", 80))
            self.assertEqual(factory.return_value.__exit__.call_count, 5)

    def test_probe_failure_closes_socket_and_returns_loopback(self):
        with patch("core.utils.util.socket.socket") as factory:
            factory.return_value.__enter__.return_value.connect.side_effect = OSError("no route")
            self.assertEqual(get_local_ip(), "127.0.0.1")
            factory.return_value.__exit__.assert_called_once()

    def test_invalid_config_does_not_silently_probe(self):
        with patch("core.utils.util.socket.socket") as factory:
            for ip in ("not-an-ip", "http://127.0.0.1", "0.0.0.0", "224.0.0.1"):
                with self.subTest(ip=ip), self.assertRaisesRegex(ValueError, "server.local_ip"):
                    get_local_ip({"server": {"local_ip": ip}})
            factory.assert_not_called()

    def test_vision_url_uses_configured_ip_but_explicit_url_wins(self):
        config = {"server": {"local_ip": "192.168.1.25", "http_port": 8003,
                             "vision_explain": "http://你的ip:端口/mcp/vision/explain"}}
        self.assertEqual(get_vision_url(config), "http://192.168.1.25:8003/mcp/vision/explain")
        config["server"]["vision_explain"] = "https://example.com/mcp/vision/explain"
        self.assertEqual(get_vision_url(config), "https://example.com/mcp/vision/explain")
