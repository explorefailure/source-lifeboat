import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from repository_backup.forgejo import ForgejoClient, ForgejoError


class MemoryForgejo(ForgejoClient):
    def __init__(self, fail_upload_once=False, max_attachment_bytes=1000):
        super().__init__("http://forgejo.test:3000", "token", "archive", max_attachment_bytes=max_attachment_bytes)
        self.releases=[]; self.assets=[]; self.bytes={}; self.next_release=1; self.next_asset=1
        self.fail_upload_once=fail_upload_once; self.upload_calls=0; self.commit_calls=0
    def ensure_repository(self, repository, description): return {"private":True,"default_branch":"main"}
    def _catalog_commit(self, repository, release_id, manifest_path, default_branch):
        self.commit_calls += 1; return "a"*40
    def _list_releases(self, repository): return list(self.releases)
    def _list_assets(self, repository, release_id): return [dict(item) for item in self.assets]
    def _delete_asset(self, repository, release_id, asset_id):
        self.assets=[item for item in self.assets if item["id"] != asset_id]
    def _upload_file(self, repository, release_id, path, name):
        self.upload_calls += 1
        if self.fail_upload_once and self.upload_calls == 2: raise ForgejoError("partial upload")
        data=path.read_bytes(); item={"id":self.next_asset,"name":name,"size":len(data),"browser_download_url":f"http://forgejo.test:3000/files/{self.next_asset}"}
        self.next_asset += 1; self.assets.append(item); self.bytes[item["browser_download_url"]]=data; return item
    def _stream_attachment_hash(self, url, max_bytes):
        data=self.bytes[url]
        if len(data)>max_bytes: raise ForgejoError("too large")
        return len(data),hashlib.sha256(data).hexdigest()
    def _request(self, method, path, value=None, expected=(200,), raw=False):
        if method=="POST" and path.endswith("/releases"):
            item={"id":self.next_release,"tag_name":value["tag_name"],"draft":True,"html_url":f"http://forgejo.test:3000/archive/repo/releases/{self.next_release}"}
            self.next_release += 1; self.releases.append(item); return item
        if method=="PATCH":
            self.releases[0]["draft"]=False; return self.releases[0]
        raise AssertionError((method,path,value))


class Response(io.BytesIO):
    status=200
    def __init__(self,data): super().__init__(data); self.headers={}
    def __enter__(self): return self
    def __exit__(self,*args): self.close()
class Opener:
    def __init__(self,data): self.data=data; self.urls=[]
    def open(self,request,timeout=None): self.urls.append(request.full_url); return Response(self.data)


class ForgejoTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); root=Path(self.temp.name)
        self.file=root/"asset.bin"; self.file.write_bytes(b"asset")
        self.manifest=root/"manifest.json"; self.manifest.write_text('{"schema_version":1}\n')
        self.release=SimpleNamespace(id=7,tag="v1")
        self.files=[SimpleNamespace(path=self.file,stored_name="asset.bin",size=5,sha256=hashlib.sha256(b"asset").hexdigest())]
    def tearDown(self): self.temp.cleanup()

    def test_create_upload_verify_and_idempotent_republish(self):
        client=MemoryForgejo(); first=client.publish("octo","demo",self.release,self.files,self.manifest)
        self.assertEqual(1,first.release_id); self.assertFalse(client.releases[0]["draft"]); self.assertEqual(2,len(client.assets))
        calls=client.upload_calls; second=client.publish("octo","demo",self.release,self.files,self.manifest)
        self.assertEqual(first.release_id,second.release_id); self.assertEqual(calls,client.upload_calls); self.assertEqual(1,client.commit_calls)

    def test_partial_failure_repairs_on_retry(self):
        client=MemoryForgejo(fail_upload_once=True)
        with self.assertRaisesRegex(ForgejoError,"partial"): client.publish("octo","demo",self.release,self.files,self.manifest)
        self.assertTrue(client.releases[0]["draft"]); self.assertEqual(1,len(client.assets))
        result=client.publish("octo","demo",self.release,self.files,self.manifest)
        self.assertEqual(1,result.release_id); self.assertEqual(2,len(client.assets))

    def test_completed_archive_corruption_is_preserved_for_investigation(self):
        client=MemoryForgejo(); client.publish("octo","demo",self.release,self.files,self.manifest)
        asset=next(item for item in client.assets if item["name"]=="asset.bin")
        client.bytes[asset["browser_download_url"]]=b"wrong"
        with self.assertRaisesRegex(ForgejoError,"checksum"):
            client.publish("octo","demo",self.release,self.files,self.manifest)
        self.assertIn("asset.bin",{item["name"] for item in client.assets})

    def test_delete_attachment_uses_release_scoped_route(self):
        class Client(ForgejoClient):
            def _request(self, method, path, **kwargs): self.called=(method,path)
        client=Client("http://forgejo.test:3000","token","archive")
        client._delete_asset("demo",7,9)
        self.assertEqual(("DELETE","/api/v1/repos/archive/demo/releases/7/assets/9"),client.called)

    def test_archive_names_distinguish_hyphenated_owner_and_repository(self):
        from repository_backup.forgejo import archive_repository_name
        self.assertNotEqual(archive_repository_name("a-b","c"),archive_repository_name("a","b-c"))
        self.assertEqual(archive_repository_name("OCTO","DEMO"),archive_repository_name("octo","demo"))

    def test_asset_and_release_lists_follow_all_pages(self):
        class Client(ForgejoClient):
            def __init__(self):
                super().__init__("http://forgejo.test:3000","token","archive")
                self.paths=[]
            def _request(self, method, path, **kwargs):
                self.paths.append(path)
                return [{"id":i} for i in range(50)] if "page=1" in path else [{"id":50}]
        client=Client()
        self.assertEqual(51,len(client._list_assets("demo",7)))
        self.assertTrue(client.paths[-1].endswith("assets?limit=50&page=2"))
        self.assertEqual(51,len(client._list_releases("demo")))
        self.assertTrue(client.paths[-1].endswith("releases?limit=50&page=2"))

    def test_limit_refused_before_publication(self):
        client=MemoryForgejo(max_attachment_bytes=4)
        with self.assertRaisesRegex(ForgejoError,"limit"): client.publish("octo","demo",self.release,self.files,self.manifest)
        self.assertEqual([],client.releases)

    def test_verification_streams_file_bytes_and_hashes(self):
        client=ForgejoClient("http://forgejo.test:3000","token","archive",opener=Opener(b"streamed"))
        size,digest=client._stream_attachment_hash("http://forgejo.test:3000/file",100)
        self.assertEqual(8,size); self.assertEqual(hashlib.sha256(b"streamed").hexdigest(),digest)
        with self.assertRaisesRegex(ForgejoError,"cross-origin"):
            client._stream_attachment_hash("http://other.test/file",100)

    def test_public_attachment_alias_rewrites_to_internal_origin(self):
        opener=Opener(b"alias")
        client=ForgejoClient(
            "http://forgejo:3000","token","archive",
            public_url="http://127.0.0.1:3000",opener=opener,
        )
        size,digest=client._stream_attachment_hash("http://127.0.0.1:3000/archive/demo/releases/download/v1/a.bin",100)
        self.assertEqual(5,size)
        self.assertEqual("http://forgejo:3000/archive/demo/releases/download/v1/a.bin",opener.urls[0])
        self.assertEqual(hashlib.sha256(b"alias").hexdigest(),digest)
        with self.assertRaisesRegex(ForgejoError,"cross-origin"):
            client._stream_attachment_hash("http://third-party.test/file",100)


if __name__ == "__main__": unittest.main()
