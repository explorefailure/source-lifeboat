import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repository_backup import oauth
from repository_backup.github import GitHubError
from repository_backup.server import GitHubSettings
import test_server

CODE = {"device_code": "d" * 40, "user_code": "ABCD-EFGH", "verification_uri": oauth.VERIFY_URL, "expires_in": 900, "interval": 5}
TOKEN = {"access_token": "gho_" + "a" * 30, "token_type": "bearer", "scope": ""}
CONNECTION = {"account": "example", "quota": {"limit": 5000, "remaining": 4999, "reset": 100}, "tested": True}


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = GitHubSettings(Path(self.temp.name))
        self.device = self.settings.device
        self.now = 100.
        self.clock = patch("repository_backup.oauth.time.monotonic", side_effect=lambda: self.now)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.temp.cleanup()

    def start(self):
        with patch("repository_backup.oauth.post", return_value=CODE) as post:
            value = self.device.action("start", {}, "owner")
            self.assertEqual({"client_id": oauth.DEFAULT_CLIENT_ID}, post.call_args.args[1])
        self.assertNotIn("device_code", json.dumps(value))
        return value

    def test_pending_slowdown_and_early_poll_are_bounded(self):
        self.start()
        with patch("repository_backup.oauth.post", return_value={"error": "slow_down", "interval": 10}) as post:
            self.device.action("poll", {}, "owner")
            self.assertFalse(post.called)
            self.now += 5
            self.assertEqual(10, self.device.action("poll", {}, "owner")["interval"])
            self.now += 9
            self.device.action("poll", {}, "owner")
            self.assertEqual(1, post.call_count)
            self.now += 1
            post.return_value = {"error": "authorization_pending"}
            self.assertEqual("pending", self.device.action("poll", {}, "owner")["state"])
            self.assertEqual(2, post.call_count)

    def test_connected_credentials_are_private_persisted_and_never_returned(self):
        self.start(); self.now += 5
        with patch("repository_backup.oauth.post", return_value=TOKEN), patch.object(self.settings, "test", return_value=CONNECTION):
            view = self.device.action("poll", {}, "owner")
        self.assertEqual("connected", view["state"])
        self.assertEqual(TOKEN["access_token"], GitHubSettings(Path(self.temp.name)).token())
        self.assertEqual(0o600, self.settings.path.stat().st_mode & 0o777)
        self.assertEqual(0o700, self.settings.folder.stat().st_mode & 0o777)
        self.assertNotIn(TOKEN["access_token"], json.dumps(view) + json.dumps(self.settings.status()))
        self.assertEqual("example", self.settings.status()["account"])

    def test_owner_session_binding_cancel_expiry_and_no_restart_resume(self):
        self.start(); self.now += 5
        with patch("repository_backup.oauth.post") as post:
            self.assertEqual("idle", self.device.action("poll", {}, "other")["state"])
            self.device.action("cancel", {}, "other")
            self.assertEqual("pending", self.device.view("owner")["state"])
            self.assertFalse(post.called)
            self.now += 900
            self.assertEqual("expired", self.device.action("poll", {}, "owner")["state"])
            self.assertNotIn("device_code", self.device.flow)
        self.assertEqual("idle", GitHubSettings(Path(self.temp.name)).device.view("owner")["state"])

    def test_remove_or_pat_save_invalidates_pending_flow(self):
        self.start()
        self.settings.save("pat_" + "a" * 30)
        self.assertIsNone(self.device.flow)
        self.settings.save(None)
        self.assertIsNone(self.settings.token())

    def test_denied_error_and_bad_identity_preserve_existing_token(self):
        self.settings.save("old_" + "b" * 30)
        for response in [{"error":"access_denied"}, {"error":"incorrect_device_code"}, TOKEN, {**TOKEN,"scope":"repo"}]:
            self.device.flow = None; self.now += 20; self.start(); self.now += 5
            with patch("repository_backup.oauth.post", return_value=response), patch.object(self.settings,"test", side_effect=GitHubError("identity failed")):
                try: self.device.action("poll", {}, "owner")
                except GitHubError: pass
            self.assertIn(self.device.view("owner")["state"], ("denied", "failed"))
            self.assertEqual("old_" + "b" * 30, self.settings.token())

    def test_client_override_persists_and_does_not_change_existing_token_app(self):
        self.start()
        self.device.action("client", {"client_id": "Ov23liOtherClient1234"}, "owner")
        self.assertIsNone(self.device.flow)
        self.assertEqual("Ov23liOtherClient1234", GitHubSettings(Path(self.temp.name)).client_id())
        with self.assertRaises(ValueError):
            self.device.action("client", {"client_id": "https://evil.test"}, "owner")

    def test_expiring_token_refresh_rotation_uses_original_app_and_identity(self):
        expiring = {**TOKEN,"expires_in":28800,"refresh_token":"ghr_" + "b" * 30,"refresh_token_expires_in":15897600}
        record = oauth.token_record(expiring, oauth.DEFAULT_CLIENT_ID)
        record.update(expires_at=0, account="example")
        self.settings.write_private(self.settings.path,record)
        updated={**expiring,"access_token":"gho_" + "c"*30,"refresh_token":"ghr_" + "d"*30}
        with patch("repository_backup.oauth.post",return_value=updated) as post, patch.object(self.settings,"test",return_value=CONNECTION):
            self.assertEqual(updated["access_token"],self.settings.token())
            self.assertEqual({"client_id":oauth.DEFAULT_CLIENT_ID,"grant_type":"refresh_token","refresh_token":expiring["refresh_token"]},post.call_args.args[1])
        self.assertEqual(updated["refresh_token"],json.loads(self.settings.path.read_text())["refresh_token"])

    def test_failed_refresh_preserves_record_and_requires_reconnect(self):
        record={"token":TOKEN["access_token"],"kind":"oauth","client_id":oauth.DEFAULT_CLIENT_ID,"expires_at":0,"refresh_expires_at":9999999999,"refresh_token":"ghr_"+"b"*30,"account":"example"}
        self.settings.write_private(self.settings.path,record)
        with patch("repository_backup.oauth.post",return_value={"error":"bad_refresh_token"}):
            with self.assertRaisesRegex(GitHubError,"Reconnect"):
                self.settings.token()
        self.assertEqual(record,json.loads(self.settings.path.read_text()))
        record["refresh_expires_at"]=0;self.settings.write_private(self.settings.path,record)
        with patch("repository_backup.oauth.post") as post:
            with self.assertRaisesRegex(GitHubError,"Reconnect"):self.settings.token()
            self.assertFalse(post.called)

    def test_malformed_device_response_and_redirect_are_rejected(self):
        for bad in [{**CODE,"verification_uri":"https://evil.test"},{**CODE,"interval":True},{**CODE,"device_code":"x"}]:
            self.now += 20
            with patch("repository_backup.oauth.post",return_value=bad), self.assertRaises(GitHubError):self.device.action("start",{},"owner")
        self.assertIsNone(oauth.NoRedirect().redirect_request(None,None,302,"",{},"https://evil.test"))


class OAuthServerTests(unittest.TestCase):
    setUp = test_server.ServerTests.setUp
    tearDown = test_server.ServerTests.tearDown
    request = test_server.ServerTests.request
    status = test_server.ServerTests.status
    login = test_server.ServerTests.login


def test_device_endpoint(self):
    path="/api/settings/github/device"
    csrf,cookies=self.login()
    headers={"Origin":self.origin,"X-CSRF-Token":csrf,"Cookie":cookies}
    self.assertEqual(401,self.request("POST",path,{"action":"start"},{"Origin":self.origin,"X-CSRF-Token":csrf,"Cookie":cookies.split("; rb_session=")[0]}).status)
    self.assertEqual(403,self.request("POST",path,{"action":"start"},{"Cookie":cookies,"Origin":self.origin}).status)
    with patch("repository_backup.oauth.post",return_value=CODE):
        response=self.request("POST",path,{"action":"start"},headers)
    self.assertEqual(200,response.status)
    self.assertNotIn(CODE["device_code"].encode(),response.body)
    self.assertEqual(200,self.request("POST",path,{"action":"cancel"},headers).status)

# Share the existing HTTP request harness.
OAuthServerTests.test_device_endpoint = test_device_endpoint
