import concurrent.futures
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import test_server
from repository_backup.auth import OwnerAuth
from repository_backup.server import Application

PASSWORD = "test-owner-password-only"


class OwnerAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "private").mkdir(mode=0o700)
        self.auth = OwnerAuth(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_password_hash_persistence_and_no_overwrite(self):
        self.auth.create(PASSWORD, PASSWORD)
        self.assertNotIn(PASSWORD, self.auth.path.read_text())
        self.assertEqual(0o600, self.auth.path.stat().st_mode & 0o777)
        fresh = OwnerAuth(self.root, "legacy-secret")
        self.assertTrue(fresh.verify(PASSWORD))
        self.assertFalse(fresh.verify("legacy-secret"))
        self.assertFalse(fresh.verify("wrong-password"))
        with self.assertRaises(FileExistsError): fresh.create("another-long-password", "another-long-password")
        self.assertTrue(fresh.verify(PASSWORD))

    def test_invalid_confirmation_and_short_password_do_not_create_owner(self):
        for password, confirmation in [("short", "short"), (PASSWORD, "different"), (None, None), ("a" * 129, "a" * 129)]:
            with self.assertRaises(ValueError): self.auth.create(password, confirmation)
            self.assertFalse(self.auth.exists())

    def test_simultaneous_claims_have_one_winner(self):
        def create(_):
            try:
                OwnerAuth(self.root).create(PASSWORD, PASSWORD)
                return True
            except FileExistsError: return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(1, sum(executor.map(create, range(2))))
        self.assertTrue(self.auth.verify(PASSWORD))

    def test_failed_login_rate_limit_expires(self):
        self.auth.create(PASSWORD, PASSWORD)
        with patch("repository_backup.auth.time.monotonic", return_value=100):
            for _ in range(5): self.assertFalse(self.auth.verify("wrong"))
            with self.assertRaisesRegex(ValueError, "Wait one minute"): self.auth.verify(PASSWORD)
        with patch("repository_backup.auth.time.monotonic", return_value=161):
            self.assertTrue(self.auth.verify(PASSWORD))


class OwnerServerTests(unittest.TestCase):
    setUp = test_server.ServerTests.setUp
    tearDown = test_server.ServerTests.tearDown
    request = test_server.ServerTests.request
    status = test_server.ServerTests.status
    login = test_server.ServerTests.login

    def new_install(self):
        self.app.close()
        config = replace(self.app.config, login_token="")
        self.app = Application(config, start_worker=False)
        status, cookie = self.status()
        self.assertTrue(status["owner_setup_needed"])
        return {"Origin":self.origin,"X-CSRF-Token":status["csrf_token"],"Cookie":cookie}

    def test_setup_creates_session_and_allows_github_settings(self):
        headers = self.new_install()
        response = self.request("POST", "/api/setup/owner", {"password":PASSWORD,"confirmation":PASSWORD}, headers)
        self.assertEqual(200,response.status)
        session=response.getheader("Set-Cookie").split(";",1)[0]
        authenticated={**headers,"Cookie":headers["Cookie"]+"; "+session}
        self.assertEqual(200,self.request("GET","/api/settings/github",headers=authenticated).status)
        self.assertFalse(json.loads(self.request("GET","/api/status",headers=authenticated).body)["owner_setup_needed"])
        self.assertEqual(409,self.request("POST","/api/setup/owner",{"password":PASSWORD,"confirmation":PASSWORD},headers).status)
        self.assertEqual(401,self.request("POST","/api/login",{"password":"wrong"},headers).status)
        self.assertEqual(200,self.request("POST","/api/login",{"password":PASSWORD},headers).status)

    def test_setup_preserves_host_origin_csrf_and_validation(self):
        headers=self.new_install();body={"password":PASSWORD,"confirmation":PASSWORD}
        self.assertEqual(403,self.request("POST","/api/setup/owner",body,{"Cookie":headers["Cookie"],"Origin":self.origin}).status)
        self.assertEqual(403,self.request("POST","/api/setup/owner",body,{**headers,"Origin":"https://evil.test"}).status)
        self.assertEqual(400,self.request("POST","/api/setup/owner",body,{**headers,"Host":"evil.test"}).status)
        self.assertEqual(400,self.request("POST","/api/setup/owner",{"password":PASSWORD,"confirmation":"wrong"},headers).status)
        self.assertFalse(self.app.owner_auth.exists())

    def test_existing_token_installation_needs_existing_key(self):
        status,cookie=self.status();headers={"Origin":self.origin,"X-CSRF-Token":status["csrf_token"],"Cookie":cookie}
        body={"password":PASSWORD,"confirmation":PASSWORD}
        self.assertTrue(status["owner_setup_key_required"])
        self.assertEqual(403,self.request("POST","/api/setup/owner",body,headers).status)
        self.assertEqual(200,self.request("POST","/api/setup/owner",{**body,"setup_key":"login-secret"},headers).status)
        self.assertFalse(self.app.owner_auth.verify("login-secret"))

    def test_remote_initialization_requires_explicit_setup_key(self):
        self.new_install()
        self.app.config=replace(self.app.config,public_url="https://lifeboat.example")
        self.host="lifeboat.example";self.origin="https://lifeboat.example"
        status,cookie=self.status();headers={"Origin":self.origin,"X-CSRF-Token":status["csrf_token"],"Cookie":cookie}
        body={"password":PASSWORD,"confirmation":PASSWORD}
        self.assertEqual(403,self.request("POST","/api/setup/owner",body,headers).status)
        with patch.dict("os.environ",{"RB_SETUP_TOKEN":"operator-setup-secret"}):
            self.assertEqual(403,self.request("POST","/api/setup/owner",body,headers).status)
            self.assertEqual(200,self.request("POST","/api/setup/owner",{**body,"setup_key":"operator-setup-secret"},headers).status)

    def test_owner_can_be_created_before_forgejo_is_ready(self):
        headers=self.new_install()
        self.app.config=replace(self.app.config,forgejo_token="")
        self.assertEqual(200,self.request("POST","/api/setup/owner",{"password":PASSWORD,"confirmation":PASSWORD},headers).status)
        self.assertTrue(self.app.config.setup_needed)
