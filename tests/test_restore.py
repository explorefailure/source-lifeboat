import hashlib
import copy
import tempfile
import unittest
from pathlib import Path

from repository_backup.manifest import read_manifest, write_manifest, same_release_contents


class RestoreTests(unittest.TestCase):
    def test_reused_snapshot_requires_complete_release_and_file_identity(self):
        original={"schema_version":1,"repository":{"owner":"a","name":"b"},"release":{"github_release_id":7,"commit_sha":"a"*40,"tag":"v1","retrieved_at":"before"},"files":[{"upstream_asset_id":1,"original_filename":"a.bin","stored_name":"a.bin","kind":"asset","size":3,"sha256":"a"*64}]}
        later=copy.deepcopy(original);later["release"]["retrieved_at"]="later"
        self.assertTrue(same_release_contents(original,later))
        for key,value in [("size",4),("sha256","b"*64),("upstream_asset_id",2),("original_filename","new.bin")]:
            changed=copy.deepcopy(later);changed["files"][0][key]=value
            self.assertFalse(same_release_contents(original,changed))
        removed=copy.deepcopy(later);removed["files"]=[]
        self.assertFalse(same_release_contents(original,removed))
        added=copy.deepcopy(later);added["files"].append({"stored_name":"extra.bin"})
        self.assertFalse(same_release_contents(original,added))

    def test_restore_to_empty_directory_and_verify_hashes(self):
        with tempfile.TemporaryDirectory() as source_name, tempfile.TemporaryDirectory() as restore_name:
            source=Path(source_name); restore=Path(restore_name)
            contents={"source.tar.gz":b"source bytes","release.bin":b"binary bytes"}
            files=[]
            for name,data in contents.items():
                (source/name).write_bytes(data)
                files.append({"kind":"asset","original_filename":name,"stored_name":name,"size":len(data),"sha256":hashlib.sha256(data).hexdigest(),"upstream_asset_id":None})
            manifest={"schema_version":1,"repository":{},"release":{},"files":files,"warnings":[]}
            write_manifest(source/"manifest.json",manifest)
            loaded=read_manifest(source/"manifest.json")
            self.assertEqual([],list(restore.iterdir()))
            for item in loaded["files"]:
                data=(source/item["stored_name"]).read_bytes(); (restore/item["stored_name"]).write_bytes(data)
                self.assertEqual(item["size"],len(data)); self.assertEqual(item["sha256"],hashlib.sha256(data).hexdigest())


if __name__ == "__main__": unittest.main()
