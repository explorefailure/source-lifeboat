import io
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import urllib.error
from pathlib import Path

from repository_backup.downloader import DownloadError, QuotaExceeded, SafeDownloader, safe_filename
from types import SimpleNamespace


class Response(io.BytesIO):
    def __init__(self, body=b"abc", status=200, headers=None):
        super().__init__(body); self.status=status; self.code=status; self.headers=headers or {}


class Opener:
    def __init__(self, values): self.values=list(values); self.requests=[]
    def open(self, request, timeout=None):
        self.requests.append(request)
        value=self.values.pop(0)
        if isinstance(value, Exception): raise value
        return value


class DownloaderTests(unittest.TestCase):
    def setUp(self): self.temp=tempfile.TemporaryDirectory()
    def tearDown(self): self.temp.cleanup()
    def downloader(self, values, **kwargs):
        kwargs.setdefault("min_free_bytes",0)
        return SafeDownloader(self.temp.name, opener=Opener(values), resolver=lambda host:["8.8.8.8"], **kwargs)

    def test_connection_pins_numeric_address_and_keeps_tls_hostname(self):
        from repository_backup.downloader import _PinnedHTTPSConnection
        context=MagicMock();sock=MagicMock()
        connection=_PinnedHTTPSConnection("api.github.com",context=context)
        with patch("repository_backup.downloader._default_resolver",return_value=["8.8.8.8"]), patch("repository_backup.downloader.socket.create_connection",return_value=sock) as connect:
            connection.connect()
        self.assertEqual(("8.8.8.8",443),connect.call_args.args[0])
        context.wrap_socket.assert_called_once_with(sock,server_hostname="api.github.com")

    def test_connection_rejects_rebound_private_address_before_connect(self):
        from repository_backup.downloader import _PinnedHTTPSConnection
        connection=_PinnedHTTPSConnection("api.github.com")
        with patch("repository_backup.downloader._default_resolver",return_value=["127.0.0.1"]), patch("repository_backup.downloader.socket.create_connection") as connect:
            with self.assertRaisesRegex(OSError,"private"):
                connection.connect()
            connect.assert_not_called()

    def test_malicious_names_are_safe_and_unique(self):
        used=set(); first=safe_filename("../../evil\x00.bin", used); second=safe_filename("../../evil\x00.bin", used)
        self.assertNotIn("/", first); self.assertNotIn("\x00", first); self.assertNotEqual(first.casefold(), second.casefold())

    def test_source_archive_uses_github_api_media_type(self):
        downloader=self.downloader([Response()])
        downloader.download("https://api.github.com/repos/a/b/tarball/abc", "source.tar.gz", kind="source")
        self.assertEqual("application/vnd.github+json", downloader.opener.requests[0].get_header("Accept"))

    def test_streams_and_hashes(self):
        item=self.downloader([Response(headers={"Content-Length":"3"})]).download("https://api.github.com/a", "a.bin", kind="asset", expected_size=3)
        self.assertEqual(3, item.size); self.assertEqual("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", item.sha256)

    def test_manifest_name_is_reserved_for_application_manifest(self):
        release=SimpleNamespace(
            source_url="https://api.github.com/source",commit_sha="a"*40,
            assets=[SimpleNamespace(url="https://api.github.com/asset",name="manifest.json",size=3,id=9)],
        )
        files=self.downloader([Response(),Response()]).download_release(release)
        self.assertEqual("manifest-2.json",files[1].stored_name)

    def test_redirect_rechecks_host_and_drops_credentials(self):
        redirect=urllib.error.HTTPError("x",302,"",{"Location":"https://objects.githubusercontent.com/file"},io.BytesIO())
        downloader=self.downloader([redirect,Response()],token="secret")
        downloader.download("https://api.github.com/a", "a", kind="asset", expected_size=3)
        self.assertIn("Authorization", downloader.opener.requests[0].headers)
        self.assertNotIn("Authorization", downloader.opener.requests[1].headers)

    def test_refuses_other_host_and_private_resolution(self):
        with self.assertRaises(DownloadError): self.downloader([]).download("https://evil.example/a","a",kind="asset")
        private=SafeDownloader(self.temp.name,opener=Opener([]),resolver=lambda host:["127.0.0.1"],min_free_bytes=0)
        with self.assertRaisesRegex(DownloadError,"private"):
            private.download("https://api.github.com/a","a",kind="asset")

    def test_partial_quota_and_checksum_fail_without_final_file(self):
        cases = [
            (dict(values=[Response(b"ab")]), dict(expected_size=3), DownloadError),
            (dict(values=[Response(b"abcd")], max_file_bytes=3), dict(), QuotaExceeded),
            (dict(values=[Response(b"abc")]), dict(expected_sha256="0"*64), DownloadError),
        ]
        for index,(constructor,arguments,error) in enumerate(cases):
            directory=Path(self.temp.name)/str(index); directory.mkdir()
            downloader=SafeDownloader(directory,opener=Opener(constructor.pop("values")),resolver=lambda host:["8.8.8.8"],min_free_bytes=0,**constructor)
            with self.subTest(index=index), self.assertRaises(error): downloader.download("https://api.github.com/a","a.bin",kind="asset",**arguments)
            self.assertEqual([], list(directory.iterdir()))

    def test_declared_total_quota_and_disk_refusal(self):
        with self.assertRaises(QuotaExceeded): self.downloader([],max_total_bytes=2).download("https://api.github.com/a","a",kind="asset",expected_size=3)
        with self.assertRaisesRegex(QuotaExceeded,"disk"):
            self.downloader([],min_free_bytes=10**30).download("https://api.github.com/a","a",kind="asset",expected_size=1)


if __name__ == "__main__": unittest.main()
