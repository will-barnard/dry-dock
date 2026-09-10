"""Self-test for checks.py. No deps, no network: `python3 check_selftest.py`."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from checks import FAIL, PASS, SKIPPED, check_vue_sfc, check_json, check_python, run_assertion

ok = bad = 0
def expect(label, got, want):
    global ok, bad
    if got == want: ok += 1; print(f"  PASS  {label}")
    else: bad += 1; print(f"  FAIL  {label}: got {got!r}, wanted {want!r}")

GOOD = """<template>
  <div class="wrap">
    <h1>{{ title }}</h1>
    <ul><li v-for="t in items" :key="t.id">{{ t.name }}</li></ul>
    <img src="x.png">
    <br/>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
const title = ref('hi')
const items = ref([])
</script>

<style scoped>
.wrap { color: red; }
</style>
"""

print("\nvalid SFC")
expect("well-formed component passes", check_vue_sfc(GOOD).verdict, PASS)
expect("void + self-closing tags ignored", check_vue_sfc(GOOD).verdict, PASS)

print("\nthe failures that actually happen")
expect("dropped closing tag",
       check_vue_sfc(GOOD.replace("</div>\n</template>", "</template>")).verdict, FAIL)
expect("two <script setup> blocks",
       check_vue_sfc(GOOD + "\n<script setup>\nconst x = 1\n</script>\n").verdict, FAIL)
expect("two <template> blocks",
       check_vue_sfc(GOOD + GOOD).verdict, FAIL)
expect("unclosed <template>",
       check_vue_sfc("<template>\n  <div>x</div>\n").verdict, FAIL)
expect("mis-nested tags",
       check_vue_sfc("<template>\n<div><span>x</div></span>\n</template>\n").verdict, FAIL)
expect("empty file", check_vue_sfc("").verdict, FAIL)
expect("prose instead of code (model ignored the format)",
       check_vue_sfc("Sure! Here is the component you asked for:\n").verdict, FAIL)

print("\nrender-function component (no template) is legal")
expect("script-only passes",
       check_vue_sfc("<script setup lang=\"ts\">\nconst a = 1\n</script>\n").verdict, PASS)

print("\nhtml comments are not structure")
expect("commented-out tag ignored",
       check_vue_sfc("<template>\n<div><!-- <span> --></div>\n</template>\n").verdict, PASS)

print("\njson / python")
expect("valid json", check_json('{"a": 1}').verdict, PASS)
expect("trailing comma", check_json('{"a": 1,}').verdict, FAIL)
expect("valid python", check_python("def f():\n    return 1\n").verdict, PASS)
expect("bad python", check_python("def f(:\n").verdict, FAIL)

print("\nassertions")
tmp = Path("/tmp/_evalselftest"); (tmp / "src").mkdir(parents=True, exist_ok=True)
(tmp / "src/A.vue").write_text("<template><div>hello</div></template>\n")
expect("file_exists hit", run_assertion(tmp, {"kind": "file_exists", "path": "src/A.vue"}).verdict, PASS)
expect("file_exists miss", run_assertion(tmp, {"kind": "file_exists", "path": "src/B.vue"}).verdict, FAIL)
expect("file_absent", run_assertion(tmp, {"kind": "file_absent", "path": "src/B.vue"}).verdict, PASS)
expect("contains hit", run_assertion(tmp, {"kind": "contains", "path": "src/A.vue", "pattern": "hello"}).verdict, PASS)
expect("contains miss", run_assertion(tmp, {"kind": "contains", "path": "src/A.vue", "pattern": "goodbye"}).verdict, FAIL)
expect("not_contains", run_assertion(tmp, {"kind": "not_contains", "path": "src/A.vue", "pattern": "goodbye"}).verdict, PASS)
expect("unknown kind is skipped, not passed",
       run_assertion(tmp, {"kind": "nonsense"}).verdict, SKIPPED)

print(f"\n{ok} passed, {bad} failed")
sys.exit(1 if bad else 0)
