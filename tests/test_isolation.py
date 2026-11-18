import re
import unittest
from pathlib import Path


ROOT=Path(__file__).parents[1]


class IsolationTests(unittest.TestCase):
    def test_compose_is_project_scoped_and_loopback_only(self):
        text=(ROOT/"compose.yaml").read_text()
        self.assertIn("codeberg.org/forgejo/forgejo:17.0.1",text)
        self.assertNotIn("container_name:",text); self.assertNotIn("external: true",text)
        self.assertNotRegex(text,r"(?m)^\s*internal:\s*true")
        self.assertIn("driver: bridge",text)
        ports=re.findall(r'"([^"\n]*:\d+)"',text)
        self.assertTrue(ports); self.assertTrue(all(value.startswith("127.0.0.1:") for value in ports),ports)
        self.assertIn("FORGEJO__actions__ENABLED: \"false\"",text); self.assertNotIn("runner",text.lower())
        self.assertEqual(2,len(re.findall(r"(?m)^\s{2}(?:app-data|forgejo-data):\s*$",text)))
        self.assertNotRegex(text,r"(?m)^\s*source:\s*/")

    def test_dockerfile_is_non_root_no_socket_and_offline_buildable(self):
        text=(ROOT/"Dockerfile").read_text()
        self.assertIn("USER repository-backup",text); self.assertIn("PYTHONPATH=/app/src",text)
        self.assertNotIn("/var/run/docker.sock",text); self.assertNotRegex(text,r"(?i)\bpip\b")

    def test_env_example_contains_only_generic_configuration(self):
        text=(ROOT/".env.example").read_text()
        for name in ("RB_STATE_DIR","RB_FORGEJO_URL","RB_FORGEJO_TOKEN","RB_FORGEJO_OWNER","RB_PUBLIC_URL","RB_INSTALLATION_ID"):
            self.assertIn(name+"=",text)
        self.assertNotRegex(text,r"(?i)(ghp_|github_pat_|[a-f0-9]{40,})")


if __name__ == "__main__": unittest.main()
