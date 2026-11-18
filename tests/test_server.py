import io
import json
import tempfile
import unittest
from unittest.mock import patch
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

from repository_backup.server import Application, Config, Handler


class Result:
    def __init__(self, raw):
        head, self.body = raw.split(b"\r\n\r\n", 1)
        lines = head.decode("iso-8859-1").split("\r\n")
        self.status = int(lines[0].split()[1])
        self.headers = Message()
        for line in lines[1:]:
            name, value = line.split(":", 1)
            self.headers.add_header(name, value.strip())

    def getheader(self, name):
        return self.headers.get(name)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        config = Config(Path(self.temp.name), "http://127.0.0.1:8080", "http://forgejo:3000", "forgejo-token", "archive", "install-1", "login-secret")
        self.app = Application(config, start_worker=False)
        self.host = "127.0.0.1:8080"
        self.origin = "http://127.0.0.1:8080"

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        encoded = b"" if body is None else json.dumps(body).encode()
        values = {"Host": self.host, **(headers or {})}
        if body is not None:
            values.update({"Content-Type": "application/json", "Content-Length": str(len(encoded))})
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(app=self.app)
        handler.command = method
        handler.path = path
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = Message()
        for name, value in values.items():
            handler.headers.add_header(name, value)
        handler.rfile = io.BytesIO(encoded)
        handler.wfile = io.BytesIO()
        handler.close_connection = True
        getattr(handler, "do_" + method)()
        return Result(handler.wfile.getvalue())

    def status(self):
        response = self.request("GET", "/api/status")
        value = json.loads(response.body)
        cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        return value, cookie

    def login(self):
        status, cookie = self.status()
        headers = {"Origin": self.origin, "X-CSRF-Token": status["csrf_token"], "Cookie": cookie}
        response = self.request("POST", "/api/login", {"token": "login-secret"}, headers)
        self.assertEqual(200, response.status)
        session = response.getheader("Set-Cookie").split(";", 1)[0]
        return status["csrf_token"], cookie + "; " + session

    def test_setup_status_starts_without_token(self):
        other = Application(Config(Path(self.temp.name) / "other", "http://127.0.0.1:8080", "http://forgejo:3000", "", "owner", "id", "login"), start_worker=False)
        self.assertTrue(other.config.setup_needed)
        other.close()

    def test_authentication_and_cookie_security(self):
        status, cookie = self.status()
        self.assertFalse(status["authenticated"])
        headers = {"Origin": self.origin, "X-CSRF-Token": status["csrf_token"], "Cookie": cookie}
        self.assertEqual(401, self.request("POST", "/api/login", {"token": "wrong"}, headers).status)
        response = self.request("POST", "/api/login", {"token": "login-secret"}, headers)
        self.assertEqual(200, response.status)
        self.assertIn("HttpOnly", response.getheader("Set-Cookie"))
        self.assertIn("SameSite=Strict", response.getheader("Set-Cookie"))

    def test_host_origin_and_csrf_rejection(self):
        self.assertEqual(400, self.request("GET", "/api/status", headers={"Host": "evil.test"}).status)
        status, cookie = self.status()
        self.assertEqual(403, self.request("POST", "/api/login", {"token": "login-secret"}, {"Origin": "http://evil.test", "X-CSRF-Token": status["csrf_token"], "Cookie": cookie}).status)
        self.assertEqual(403, self.request("POST", "/api/login", {"token": "login-secret"}, {"Origin": self.origin, "Cookie": cookie}).status)

    def test_url_validation_and_no_browser_endpoint(self):
        csrf, cookies = self.login()
        headers = {"Origin": self.origin, "X-CSRF-Token": csrf, "Cookie": cookies}
        response = self.request("POST", "/api/repositories", {"url": "http://evil.test/x/y"}, headers)
        self.assertEqual(400, response.status)
        self.assertIn(b"only https", response.body)
        self.assertEqual(404, self.request("POST", "/api/chat", {}, headers).status)

    def test_repeated_submissions_are_keyed_by_resolved_release(self):
        csrf,cookies=self.login()
        headers={"Origin":self.origin,"X-CSRF-Token":csrf,"Cookie":cookies}
        release=SimpleNamespace(id=7,tag="v1",commit_sha="a"*40,assets=[])
        with patch("repository_backup.server.GitHubClient.latest_release",return_value=release):
            first=self.request("POST","/api/repositories",{"url":"https://github.com/a/b"},headers)
            second=self.request("POST","/api/repositories",{"url":"https://github.com/a/b"},headers)
        self.assertEqual(202,first.status)
        self.assertEqual(json.loads(first.body)["job"]["id"],json.loads(second.body)["job"]["id"])
        self.assertEqual(1,len(self.app.catalog.list_jobs()))

    def test_settings_authentication_and_csrf(self):
        self.assertEqual(401,self.request("GET","/api/settings/github").status)
        csrf,cookie=self.status()
        headers={"Origin":self.origin,"X-CSRF-Token":csrf["csrf_token"],"Cookie":cookie}
        self.assertEqual(401,self.request("POST","/api/settings/github",{"action":"remove"},headers).status)
        token,cookies=self.login()
        self.assertEqual(403,self.request("POST","/api/settings/github",{"action":"remove"},{"Origin":self.origin,"Cookie":cookies}).status)
        self.assertEqual(403,self.request("POST","/api/settings/github",{"action":"remove"},{"Origin":"http://evil.example","X-CSRF-Token":token,"Cookie":cookies}).status)

    def test_settings_round_trip_permissions_and_secret_redaction(self):
        from repository_backup.server import GitHubSettings
        import stat
        csrf,cookies=self.login();headers={"Origin":self.origin,"X-CSRF-Token":csrf,"Cookie":cookies}
        token="github_pat_"+"a"*40
        def response(client,path):
            self.assertEqual(token,client.token)
            return {"login":"example-user"} if path=="/user" else {"resources":{"core":{"limit":5000,"remaining":4998,"reset":2000000000}}}
        with patch("repository_backup.server.GitHubClient._json",autospec=True,side_effect=response):
            saved=self.request("POST","/api/settings/github",{"action":"save","token":token},headers)
            self.assertEqual(200,saved.status)
            self.assertEqual("example-user",json.loads(saved.body)["account"])
            self.assertNotIn(token.encode(),saved.body)
            self.assertEqual(200,self.request("POST","/api/settings/github",{"action":"test"},headers).status)
        self.assertEqual(token,GitHubSettings(Path(self.temp.name)).token())
        private=Path(self.temp.name)/"private"
        self.assertEqual(0o700,stat.S_IMODE(private.stat().st_mode))
        self.assertEqual(0o600,stat.S_IMODE((private/"github.json").stat().st_mode))
        status=self.request("GET","/api/settings/github",headers={"Cookie":cookies})
        self.assertNotIn(token.encode(),status.body)
        self.assertTrue(json.loads(status.body)["configured"])
        with patch.dict("os.environ",{"RB_GITHUB_TOKEN":"environment-token"}):
            removed=self.request("POST","/api/settings/github",{"action":"remove"},headers)
            self.assertFalse(json.loads(removed.body)["configured"])
            self.assertIsNone(self.app.github_settings.token())
        self.assertNotIn(token,(private/"github.json").read_text())
        self.assertNotIn(token.encode(),(Path(self.temp.name)/"catalog.sqlite3").read_bytes())

    def test_bad_connection_keeps_previous_credentials(self):
        from repository_backup.github import GitHubError
        self.app.github_settings.save("previous-token-value-12345")
        csrf,cookies=self.login();headers={"Origin":self.origin,"X-CSRF-Token":csrf,"Cookie":cookies}
        token="github_pat_"+"b"*40
        with patch("repository_backup.server.GitHubClient._json",side_effect=GitHubError(token)):
            response=self.request("POST","/api/settings/github",{"action":"save","token":token},headers)
        self.assertEqual(502,response.status)
        self.assertNotIn(token.encode(),response.body)
        self.assertEqual("previous-token-value-12345",self.app.github_settings.token())
        self.assertEqual(400,self.request("POST","/api/settings/github",{"action":"save","token":"bad\nvalue"},headers).status)

    def test_repository_check_uses_saved_token(self):
        self.app.github_settings.save("saved-token-value-12345")
        csrf,cookies=self.login();headers={"Origin":self.origin,"X-CSRF-Token":csrf,"Cookie":cookies}
        with patch("repository_backup.server.GitHubClient") as client:
            client.return_value.latest_release.return_value=SimpleNamespace(id=8,tag="v2",commit_sha="b"*40,assets=[])
            response=self.request("POST","/api/repositories",{"url":"https://github.com/a/b"},headers)
        self.assertEqual(202,response.status)
        client.assert_called_once_with(token="saved-token-value-12345")

    def test_inflight_job_keeps_one_token_when_settings_change(self):
        self.app.github_settings.save("first-token-value-12345")
        repo=self.app.catalog.add_repository("a","b","https://github.com/a/b")
        job=self.app.catalog.create_job(repo,"test-release")
        def resolve(repository):
            self.app.github_settings.save(None)
            return SimpleNamespace(id=8)
        with patch("repository_backup.server.GitHubClient") as github, patch("repository_backup.server.SafeDownloader",side_effect=RuntimeError("test download boundary")) as downloader:
            github.return_value.latest_release.side_effect=resolve
            with self.assertRaisesRegex(RuntimeError,"test download boundary"):
                self.app._process_job(job,lambda *args:None,lambda:None)
            github.assert_called_once_with(token="first-token-value-12345")
            self.assertEqual("first-token-value-12345",downloader.call_args.kwargs["token"])
        self.assertIsNone(self.app.github_settings.token())

    def test_download_settings_persist_and_preserve_existing_files(self):
        from repository_backup.server import DownloadSettings
        csrf,cookies=self.login();headers={"Origin":self.origin,"X-CSRF-Token":csrf,"Cookie":cookies}
        existing=Path(self.temp.name)/"snapshots"/"old.bin"
        existing.parent.mkdir();existing.write_bytes(b"original")
        destination=Path(self.temp.name)/"new-downloads"
        response=self.request("POST","/api/settings/downloads",{"folder":str(destination)},headers)
        self.assertEqual(200,response.status)
        self.assertEqual(str(destination),json.loads(response.body)["folder"])
        self.assertEqual(destination,DownloadSettings(Path(self.temp.name)).root())
        self.assertEqual(b"original",existing.read_bytes())
        self.assertTrue((destination/"staging").is_dir())
        self.assertTrue((destination/"snapshots").is_dir())
        self.assertEqual(200,self.request("GET","/api/settings/downloads",headers={"Cookie":cookies}).status)
        reset=self.request("POST","/api/settings/downloads",{"folder":self.temp.name},headers)
        self.assertFalse(json.loads(reset.body)["custom"])
        self.assertTrue(destination.is_dir())

    def test_download_settings_auth_validation_and_failed_write(self):
        self.assertEqual(401,self.request("GET","/api/settings/downloads").status)
        csrf,cookies=self.login();headers={"Origin":self.origin,"X-CSRF-Token":csrf,"Cookie":cookies}
        self.assertEqual(403,self.request("POST","/api/settings/downloads",{"folder":self.temp.name},{"Cookie":cookies,"Origin":self.origin}).status)
        for folder in ["relative/path","",str(Path(self.temp.name)/"private"),str(Path(self.temp.name)/"private"/"inside")]:
            self.assertEqual(400,self.request("POST","/api/settings/downloads",{"folder":folder},headers).status)
        blocked=Path(self.temp.name)/"blocked";blocked.mkdir();(blocked/"staging").symlink_to(Path(self.temp.name)/"private",target_is_directory=True)
        self.assertEqual(400,self.request("POST","/api/settings/downloads",{"folder":str(blocked)},headers).status)
        with patch("repository_backup.server.tempfile.TemporaryFile",side_effect=PermissionError("no write access")):
            self.assertEqual(400,self.request("POST","/api/settings/downloads",{"folder":str(Path(self.temp.name)/"unwritable")},headers).status)
        self.assertEqual(Path(self.temp.name),self.app.download_settings.root())

    def test_running_job_uses_one_download_folder(self):
        original=Path(self.temp.name)/"first-downloads"
        self.app.download_settings.save(str(original))
        repo=self.app.catalog.add_repository("a","b","https://github.com/a/b")
        job=self.app.catalog.create_job(repo,"test-download-folder")
        def resolve(repository):
            self.app.download_settings.save(str(Path(self.temp.name)/"later-downloads"))
            return SimpleNamespace(id=8)
        with patch("repository_backup.server.GitHubClient") as github, patch("repository_backup.server.SafeDownloader",side_effect=RuntimeError("test boundary")) as downloader:
            github.return_value.latest_release.side_effect=resolve
            with self.assertRaisesRegex(RuntimeError,"test boundary"):
                self.app._process_job(job,lambda *args:None,lambda:None)
            self.assertEqual(original/"staging",downloader.call_args.args[0].parent)

    def test_settings_page_route(self):
        response=self.request("GET","/settings")
        self.assertEqual(200,response.status)
        self.assertIn(b"GitHub connection",response.body)

    def test_static_ui_uses_inert_text_rendering(self):
        response = self.request("GET", "/static/app.js")
        self.assertEqual(200, response.status)
        self.assertNotIn(b"innerHTML", response.body)
        self.assertIn(b"textContent", response.body)
        response = self.request("GET", "/")
        self.assertEqual(200, response.status)
        self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy"))


if __name__ == "__main__":
    unittest.main()
