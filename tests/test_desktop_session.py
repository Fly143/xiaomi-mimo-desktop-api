import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.desktop_session import read_desktop_pass_token


class DesktopSessionTests(unittest.TestCase):
    def test_reads_macos_cookie_database_from_partition_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cookie_path = (
                Path(temp_dir)
                / "Library/Application Support/Xiaomi MiMo/Partitions/xiaomi-account/Cookies"
            )
            cookie_path.parent.mkdir(parents=True)
            with sqlite3.connect(cookie_path) as conn:
                conn.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT)")
                conn.execute(
                    "INSERT INTO cookies VALUES (?, ?, ?)",
                    (".account.xiaomi.com", "passToken", "test-pass-token"),
                )
                conn.execute(
                    "INSERT INTO cookies VALUES (?, ?, ?)",
                    (".account.xiaomi.com", "userId", "test-user"),
                )
                conn.commit()

            with patch(
                "app.desktop_session.Path.home", return_value=Path(temp_dir)
            ), patch(
                "app.desktop_session.sys_platform_is_darwin", return_value=True
            ):
                result = read_desktop_pass_token()

        self.assertEqual(
            result,
            {
                "passToken": "test-pass-token",
                "userId": "test-user",
                "cUserId": None,
            },
        )


if __name__ == "__main__":
    unittest.main()
