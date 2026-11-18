import io
import json
import unittest
import urllib.error

from repository_backup.github import GitHubClient, GitHubError, NoRelease, parse_repository_url


class Response(io.BytesIO):
    def __init__(self, value, status=200):
        super().__init__(json.dumps(value).encode())
        self.status = status
        self.headers = {}
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class Opener:
    def __init__(self, responses): self.responses = responses
    def open(self, request, timeout=None):
        path = request.full_url.removeprefix("https://api.github.com")
        value = self.responses[path]
        if isinstance(value, Exception): raise value
        return Response(value)


def release(**changes):
    value = {
        "id": 7, "tag_name": "v1.0", "draft": False, "prerelease": False,
        "published_at": "2026-01-01T00:00:00Z",
        "html_url": "https://github.com/octo/demo/releases/tag/v1.0",
        "assets": [{"id": 8, "name": "demo.bin", "size": 3, "url": "https://api.github.com/repos/octo/demo/releases/assets/8"}],
    }
    value.update(changes)
    return value


class GitHubTests(unittest.TestCase):
    def test_url_validation(self):
        good = parse_repository_url("https://github.com/octo/demo")
        self.assertEqual(("octo", "demo"), (good.owner, good.name))
        for bad in (
            "http://github.com/octo/demo", "https://evil.example/octo/demo",
            "https://user:pass@github.com/octo/demo", "https://github.com/octo/demo/extra",
            "https://github.com/octo/demo?x=1", "https://github.com/octo/demo.git", " https://github.com/octo/demo",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError): parse_repository_url(bad)

    def client(self, latest=None, ref=None, tag=None):
        responses = {
            "/repos/octo/demo/releases/latest": latest or release(),
            "/repos/octo/demo/git/ref/tags/v1.0": ref or {"object": {"type": "commit", "sha": "a" * 40}},
        }
        assets=(latest.get("assets",[]) if isinstance(latest,dict) else release()["assets"])
        responses["/repos/octo/demo/releases/7/assets?per_page=100&page=1"]=assets
        if tag is not None: responses["/repos/octo/demo/git/tags/" + "b" * 40] = tag
        return GitHubClient(opener=Opener(responses))

    def test_resolves_published_release_and_assets(self):
        result = self.client().latest_release("https://github.com/octo/demo")
        self.assertEqual("a" * 40, result.commit_sha)
        self.assertEqual(8, result.assets[0].id)

    def test_assets_are_loaded_from_all_pages_not_embedded_array(self):
        client=self.client(latest=release(assets=[]))
        def asset(i):return {"id":i,"name":f"file-{i}.bin","size":3,"url":f"https://api.github.com/repos/octo/demo/releases/assets/{i}"}
        client.opener.responses["/repos/octo/demo/releases/7/assets?per_page=100&page=1"]=[asset(i) for i in range(100)]
        client.opener.responses["/repos/octo/demo/releases/7/assets?per_page=100&page=2"]=[asset(100)]
        result=client.latest_release("https://github.com/octo/demo")
        self.assertEqual(101,len(result.assets))
        self.assertEqual(100,result.assets[-1].id)

    def test_peels_annotated_tag(self):
        result = self.client(
            ref={"object": {"type": "tag", "sha": "b" * 40}},
            tag={"object": {"type": "commit", "sha": "c" * 40}},
        ).latest_release("https://github.com/octo/demo")
        self.assertEqual("c" * 40, result.commit_sha)

    def test_draft_and_prerelease_are_no_release(self):
        for value in (release(draft=True), release(prerelease=True)):
            with self.subTest(value=value), self.assertRaises(NoRelease): self.client(latest=value).latest_release("https://github.com/octo/demo")

    def test_404_is_no_release(self):
        error = urllib.error.HTTPError("x", 404, "missing", {}, io.BytesIO())
        with self.assertRaises(NoRelease): self.client(latest=error).latest_release("https://github.com/octo/demo")

    def test_changed_asset_is_stale(self):
        client = self.client()
        expected = client.latest_release("https://github.com/octo/demo")
        client.opener.responses["/repos/octo/demo/releases/latest"] = release(assets=[])
        client.opener.responses["/repos/octo/demo/releases/7/assets?per_page=100&page=1"] = []
        with self.assertRaisesRegex(GitHubError, "changed"):
            client.assert_unchanged("https://github.com/octo/demo", expected)


if __name__ == "__main__": unittest.main()
