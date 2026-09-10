"""Regression tests for the SEARCH/REPLACE parser.

Exists because of a bug the eval harness surfaced: the format example the
model is shown was indented four spaces, models copied that indentation, and
the parser carried it into the block bodies. Every edit to an existing file
then failed with "SEARCH not found" — which looked exactly like the model
hallucinating file contents, and was not.

Run: python3 parser_selftest.py     (no deps, no model, no network)
"""
import importlib.util, sys, types
from pathlib import Path

WORKER = Path(__file__).resolve().parent.parent

class _L:
    def __getattr__(self, _): return lambda *a, **k: None
_m = types.ModuleType("structlog"); _m.get_logger = lambda *a, **k: _L()
sys.modules["structlog"] = _m
class _S: max_context = 32768; num_predict = 8192; prompt_budget_ratio = 0.7
for _n, _a in [("app", {}), ("app.runners", {}), ("app.config", {"get_settings": lambda: _S()}),
               ("app.git_workspace", {"GitWorkspace": type("G", (), {})}),
               ("app.ollama_client", {"get_provider": lambda: None})]:
    _mod = types.ModuleType(_n)
    for _k, _v in _a.items(): setattr(_mod, _k, _v)
    _mod.__path__ = []
    sys.modules[_n] = _mod
_spec = importlib.util.spec_from_file_location("app.runners.base", WORKER / "app/runners/base.py")
base = importlib.util.module_from_spec(_spec); sys.modules["app.runners.base"] = base
_spec.loader.exec_module(base)

ok = bad = 0
def check(label, cond, detail=""):
    global ok, bad
    if cond: ok += 1; print(f"  PASS  {label}")
    else: bad += 1; print(f"  FAIL  {label} {detail}")

FILE = "const greeting = computed(() => `Hello, ${props.name}`)\n"

print("\n1. indented block (a model copying an indented example)")
resp = """I'll update the greeting.

    src/components/HelloWorld.vue
    <<<<<<< SEARCH
    const greeting = computed(() => `Hello, ${props.name}`)
    =======
    const greeting = computed(() => `Hello, ${props.name}!`)
    >>>>>>> REPLACE
"""
b = base.extract_search_replace_blocks(resp)
check("one block parsed", len(b) == 1, f"got {len(b)}")
check("filename correct", b and b[0][0] == "src/components/HelloWorld.vue")
check("SEARCH is dedented and matches the real file", b and b[0][1] in FILE,
      repr(b[0][1]) if b else "")
check("REPLACE is dedented", b and not b[0][2].startswith("    "))

print("\n2. correctly formatted block at column zero is untouched")
resp = """src/a.py
<<<<<<< SEARCH
def f():
    return 1
=======
def f():
    return 2
>>>>>>> REPLACE
"""
b = base.extract_search_replace_blocks(resp)
check("one block parsed", len(b) == 1)
check("inner indentation preserved", b and b[0][1] == "def f():\n    return 1\n",
      repr(b[0][1]) if b else "")
check("replacement indentation preserved", b and b[0][2] == "def f():\n    return 2\n")

print("\n3. new file (empty SEARCH), indented")
resp = """    src/components/TaskList.vue
    <<<<<<< SEARCH
    =======
    <template>
      <ul><li v-for="t in tasks">{{ t.label }}</li></ul>
    </template>
    >>>>>>> REPLACE
"""
b = base.extract_search_replace_blocks(resp)
check("SEARCH is empty (a create)", b and not b[0][1].strip())
check("content starts flush left", b and b[0][2].startswith("<template>"),
      repr(b[0][2][:30]) if b else "")
check("inner template indentation kept", b and "  <ul>" in b[0][2])
sys.path.insert(0, str(Path(__file__).parent))
from checks import check_vue_sfc
check("and the result passes tier-0", b and check_vue_sfc(b[0][2]).verdict == "pass",
      check_vue_sfc(b[0][2]).detail if b else "")

print("\n4. tab-indented block")
resp = "\tsrc/a.py\n\t<<<<<<< SEARCH\n\tx = 1\n\t=======\n\tx = 2\n\t>>>>>>> REPLACE\n"
b = base.extract_search_replace_blocks(resp)
check("tabs stripped too", b and b[0][1] == "x = 1\n", repr(b[0][1]) if b else "")

print("\n5. multiple blocks, mixed indentation")
resp = """src/a.py
<<<<<<< SEARCH
a = 1
=======
a = 2
>>>>>>> REPLACE

  src/b.py
  <<<<<<< SEARCH
  b = 1
  =======
  b = 2
  >>>>>>> REPLACE
"""
b = base.extract_search_replace_blocks(resp)
check("both blocks parsed", len(b) == 2, f"got {len(b)}")
check("first untouched", b and b[0][1] == "a = 1\n")
check("second dedented", len(b) > 1 and b[1][1] == "b = 1\n", repr(b[1][1]) if len(b) > 1 else "")

print("\n6. inside a fenced code block (models fence constantly)")
resp = "```\nsrc/a.py\n<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n```\n"
b = base.extract_search_replace_blocks(resp)
check("fence stripped, block found", len(b) == 1 and b[0][1] == "x = 1\n")

print("\n7. the instructions no longer show an indented example")
lines = base.SEARCH_REPLACE_INSTRUCTIONS.splitlines()
markers = [l for l in lines if l.lstrip().startswith(("<<<<<<<", "=======", ">>>>>>>"))]
check("all marker lines flush left", markers and all(not l.startswith((" ", "\t")) for l in markers),
      repr(markers))

print(f"\n{ok} passed, {bad} failed")
sys.exit(1 if bad else 0)
