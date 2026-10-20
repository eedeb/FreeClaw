"""The Developer app's project map (src/dev_map.py and the overview, outline
and find tools): small answers about a big project, without reading it.

    python tests/test_dev_map.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import src.dev_map as dev_map        # noqa: E402
import src.dev_server as dev_server  # noqa: E402

FILES = {
    "README.md": "# Shop\n\nA tiny shop backend.\n\n## Setup\n\n```\n# not a heading\n```\n",
    "package.json": '{"name": "shop", "scripts": {"dev": "vite", "test": "vitest"}, "dependencies": {"react": "1"}}',
    "app.py": "import os\n\nclass Cart:\n    def add(self, item):\n        pass\n\n    def total(self):\n        return 0\n\n"
              "async def checkout(cart, card):\n    pass\n",
    "web/cart.ts": "export interface Item { id: string }\nexport class CartView {\n  render() {\n    if (x) {}\n  }\n}\n"
                   "export const priceOf = (item: Item) => 1\nexport default function App() {}\n",
    "svc/main.go": "package main\n\ntype Order struct{}\n\nfunc (o *Order) Pay() error { return nil }\n\nfunc main() {}\n",
    "lib/money.rs": "pub struct Money(i64);\nimpl Money {\n    pub fn cents(&self) -> i64 { self.0 }\n}\n",
    "broken.py": "def ok():\n    pass\n\ndef bad(:\n",
    "node_modules/huge/index.js": "function hidden() {}\n",
    "dist/bundle.js": "function alsoHidden() {}\n",
}


class MapTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="fc-map-")
        self.proj = os.path.join(self.root, "shop")
        for rel, text in FILES.items():
            path = os.path.join(self.proj, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)
        env = mock.patch.dict(os.environ, {"FC_DEV_WORKSPACE": self.root})
        env.start()
        self.addCleanup(env.stop)
        self.tools = dev_server.Tools()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def call(self, tool, **args):
        return self.tools.call(tool, args)

    def test_overview(self):
        out = self.call("overview", path="shop")
        self.assertIn("Languages:", out)
        self.assertIn("A tiny shop backend.", out)
        self.assertIn("scripts: dev, test", out)
        self.assertIn("shop/app.py", out)                     # an entry point
        self.assertNotIn("hidden", out)
        self.assertLess(len(out), 3000)

    def test_outline_python_with_methods(self):
        out = self.call("outline", path="shop/app.py")
        self.assertIn("     3  class Cart", out)
        self.assertIn("     4    def add(self, item)", out)  # nested under Cart
        self.assertIn("async def checkout(cart, card)", out)
        self.assertNotIn("pass", out)                         # no bodies

    def test_outline_other_languages(self):
        ts = self.call("outline", path="shop/web/cart.ts")
        for want in ("export interface Item", "export class CartView", "render()", "export const priceOf",
                     "export default function App()"):
            self.assertIn(want, ts)
        self.assertNotIn("if (x)", ts)
        go = self.call("outline", path="shop/svc/main.go")
        self.assertIn("func (o *Order) Pay() error", go)
        self.assertIn("type Order struct", go)
        rs = self.call("outline", path="shop/lib/money.rs")
        self.assertIn("pub fn cents(&self) -> i64", rs)
        md = self.call("outline", path="shop/README.md")
        self.assertIn("## Setup", md)
        self.assertNotIn("not a heading", md)                # inside a code fence

    def test_broken_python_still_outlined(self):
        out = self.call("outline", path="shop/broken.py")
        self.assertIn("def ok()", out)

    def test_folder_outline_skips_dependencies_and_builds(self):
        out = self.call("outline", path="shop")
        self.assertIn("shop/app.py", out)
        self.assertNotIn("node_modules", out)
        self.assertNotIn("dist/", out)
        self.assertNotIn("def add", out)                     # a folder view is top level only

    def test_find(self):
        self.assertIn("shop/app.py:4  def add(self, item)", self.call("find", name="add", path="shop"))
        self.assertIn("shop/svc/main.go:5", self.call("find", name="Pay"))
        self.assertIn("shop/app.py:3  class Cart", self.call("find", name="cart"))
        partial = self.call("find", name="pri", partial=True)
        self.assertIn("priceOf", partial)
        self.assertIn("No definition", self.call("find", name="nothingLikeThis"))

    def test_search_files_only_and_git_ignore(self):
        subprocess.run(["git", "init", "-q"], cwd=self.proj,
                       env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"}, check=True)
        with open(os.path.join(self.proj, ".gitignore"), "w") as f:
            f.write("web/\n")
        out = self.call("search", pattern="def|function", path="shop", files_only=True)
        self.assertIn("shop/app.py (", out)
        self.assertNotIn("web/cart.ts", out)                 # ignored by the repo
        self.assertNotIn("node_modules", out)

    def test_cached_until_changed(self):
        path = os.path.join(self.proj, "app.py")
        first = dev_map.symbols(path)
        self.assertIs(dev_map.symbols(path), first)
        with open(path, "a") as f:
            f.write("\ndef later():\n    pass\n")
        self.assertTrue(any(s[3] == "later" for s in dev_map.symbols(path)))

    def test_fenced_to_the_workspace(self):
        with self.assertRaises(dev_server.ToolError):
            self.call("outline", path="../")


if __name__ == "__main__":
    unittest.main(verbosity=1)
