from __future__ import annotations

import socket
import unittest
from unittest.mock import patch

from ds940.discovery import discover_scanner_ip


class DiscoveryTests(unittest.TestCase):
    @patch("socket.create_connection")
    def test_preferred_ip_success(self, mock_conn):
        mock_conn.return_value.__enter__.return_value = None
        ip = discover_scanner_ip("192.168.0.29")
        self.assertEqual(ip, "192.168.0.29")

    @patch("socket.create_connection", side_effect=OSError("timed out"))
    @patch("socket.gethostbyname")
    def test_mdns_fallback(self, mock_gethostbyname, mock_conn):
        mock_gethostbyname.side_effect = lambda h: "192.168.0.29" if "BR5CF370DC7026" in h else None
        ip = discover_scanner_ip("192.168.0.25")
        self.assertEqual(ip, "192.168.0.29")

    @patch("socket.create_connection", side_effect=OSError("timed out"))
    @patch("socket.gethostbyname", side_effect=socket.gaierror)
    def test_unreachable(self, mock_gethostbyname, mock_conn):
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = mock_popen.return_value
            mock_proc.communicate.return_value = ("", "")
            ip = discover_scanner_ip("192.168.0.25")
            self.assertIsNone(ip)


if __name__ == "__main__":
    unittest.main()
