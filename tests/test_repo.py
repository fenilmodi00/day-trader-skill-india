import json
import os
import re
import subprocess
import unittest

from _load import ROOT

# the India port: ten skills, Upstox, IST, INR
SKILLS = ["trader-scanner", "trader-news", "trader-entry", "trader-exit", "trader-timemachine",
          "trader-risk", "trader-budget", "trader-broker", "trader-alerts", "trader-analyst"]
ABOUT = ("10 Claude skills for day trading Indian markets: scan the NSE universe, track Indian "
         "news, time every entry and exit in IST, and replay every outcome in INR before you risk "
         "a rupee, on Upstox with sandbox trading by default.")
REPO = "https://github.com/fenilmodi00/day-trader-skill-india"
# skills on the way to an order carry the safety block
ORDER_PATH = ["trader-scanner", "trader-entry", "trader-exit", "trader-timemachine", "trader-budget",
              "trader-risk", "trader-broker", "trader-alerts"]
# US-only names that must not survive the port; each assembled from fragments so
# this scan does not trip over its own ban list, the same trick as the email check below.
# All are matched as words only, so dates and ordinary words do not trip them.
US_WORDS = ["alp" "aca", "ib" "kr", "k" "ite", "nas" "daq", "ed" "gar",
            "house " "clerk", "p" "dt", "\bse" "c\b", "\bpel" "osi\b"]
# where the US names may appear: the README's attribution and out-of-scope note, the LICENSE fork notice
ALLOWED_IN = ("README.md", "LICENSE")


def frontmatter(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return (m.group(1) if m else ""), text


def every_text_file():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for f in files:
            if f.endswith((".md", ".py", ".json", ".csv", ".txt", ".xml", ".yml")) or f == "LICENSE":
                yield os.path.join(base, f)


def us_word_pattern():
    parts = []
    for w in US_WORDS:
        parts.append(w if w.startswith("\\b") else r"\b%s\b" % re.escape(w))
    return re.compile("|".join(parts), re.I)


class Repo(unittest.TestCase):
    def test_ten_skill_folders_at_the_root(self):
        found = sorted(d for d in os.listdir(ROOT)
                       if os.path.isfile(os.path.join(ROOT, d, "SKILL.md")))
        self.assertEqual(found, sorted(SKILLS))

    def test_root_matches_the_india_layout(self):
        # `trader/` is the ignored runtime state dir created by live runs, not layout.
        root = sorted(d for d in os.listdir(ROOT)
                      if not d.startswith(".git") and d != "trader")
        self.assertEqual(root, sorted([".claude-plugin", "LICENSE", "README.md", "tests",
                                       "trader_core"] + SKILLS))
        # hidden entries the filter above skips
        self.assertTrue(os.path.isdir(os.path.join(ROOT, ".github")), ".github")
        self.assertTrue(os.path.isfile(os.path.join(ROOT, ".gitignore")), ".gitignore")

    def test_no_us_only_skill_folders_are_left(self):
        for old in ("trader-pelosi", "trader-india"):
            self.assertFalse(os.path.isdir(os.path.join(ROOT, old)), old)

    def test_frontmatter_name_and_description(self):
        for s in SKILLS:
            fm, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            self.assertIn("name: %s\n" % s, fm + "\n", s)
            self.assertRegex(fm, r"description: (>-?\s*)\n", s)
            self.assertGreater(len(fm), 200, "%s description is too thin" % s)
            self.assertGreater(len(text.splitlines()), 60, s)

    def test_order_path_skills_carry_the_safety_rule(self):
        for s in ORDER_PATH:
            _, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            low = " ".join(text.lower().split())
            self.assertIn("not financial advice", low, s)
            self.assertIn("yes", low, s)

    def test_plugin_manifest_points_at_the_india_repo(self):
        with open(os.path.join(ROOT, ".claude-plugin", "plugin.json")) as fh:
            plugin = json.load(fh)
        self.assertEqual(plugin["name"], "day-trader-skill-india")
        self.assertEqual(plugin["version"], "3.0.0")
        self.assertEqual(plugin["skills"], "./")
        self.assertEqual(plugin["description"], ABOUT)
        self.assertEqual(plugin["homepage"], REPO)
        self.assertEqual(plugin["repository"], REPO)
        for key in ("india", "nse", "upstox", "ist", "inr"):
            self.assertIn(key, plugin["keywords"], key)
        with open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")) as fh:
            market = json.load(fh)
        self.assertEqual(market["name"], "day-trader-skill-india")
        self.assertEqual(market["plugins"][0]["name"], "day-trader-skill-india")
        self.assertEqual(market["plugins"][0]["source"], "./")
        self.assertEqual(market["plugins"][0]["category"], "productivity")
        self.assertEqual(market["plugins"][0]["homepage"], REPO)

    def test_readme_carries_the_india_copy(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = " ".join(fh.read().split())
        self.assertTrue(readme.startswith("# The Day Trader skill, India"))
        for line in (ABOUT,
                     REPO + " install skill",
                     "/plugin marketplace add fenilmodi00/day-trader-skill-india",
                     "/plugin install day-trader-skill-india@day-trader-skill-india",
                     "UPSTOX_API_KEY", "UPSTOX_API_SECRET", "UPSTOX_ACCESS_TOKEN",
                     "UPSTOX_SANDBOX_TOKEN", "UPSTOX_ANALYTICS_TOKEN",
                     "GO LIVE WITH UPSTOX", "03:30 IST",
                     "1 minute | 1 month", "10 years", "no historical 5-minute series",
                     "09:00 | pre-open", "09:15 | the market opens",
                     "15:00 | the entry cutoff", "15:10 | flat-by", "15:25 | the broker's MIS square-off",
                     "STT", "SEBI", "GST", "stamp", "## Tests", "## Fine print" if False else "India fine print",
                     "## Fine print" if False else "Not financial advice"):
            self.assertIn(line, readme)
        self.assertIn("sandbox", readme.lower())
        self.assertIn("Sandbox is the default", readme)
        self.assertIn("Jake Schincariol", readme)
        self.assertIn("Jakeschincariol/day-trader-skill", readme)
        for s in SKILLS:
            self.assertIn("/" + s, readme, s)

    def test_no_em_dashes_and_no_personal_email(self):
        for path in every_text_file():
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            rel = os.path.relpath(path, ROOT)
            if not rel.startswith(os.path.join("tests", "fixtures")):  # fixtures are real data, kept as published
                self.assertNotIn("\u2014", text, rel)
            self.assertNotIn("jakeschincariol" + "@" + "gmail.com", text, rel)  # built, so this file passes too

    @staticmethod
    def _us_context_ok(low):
        # a US name is allowed only where the README/LICENSE names the original
        # repo, its US-market scope, or the retired tools as out of scope
        return any(t in low for t in ("jakeschincariol/day-trader-skill", "out of scope",
                                      "original ships the us-market version",
                                      "original pack"))

    def test_no_us_only_strings_outside_the_attribution_note(self):
        pat = us_word_pattern()
        for path in every_text_file():
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            rel = os.path.relpath(path, ROOT)
            norm = rel.replace(os.sep, "/")
            if norm.startswith("tests/fixtures"):
                continue  # fixtures are real data, kept as published
            if norm.startswith("tests/") and norm != "tests/test_repo.py":
                continue  # sibling test modules are not this ticket's files; they may name banned words in their ban lists
            if norm in ALLOWED_IN:
                for line in text.splitlines():
                    if pat.search(line):
                        self.assertTrue(self._us_context_ok(line.lower()),
                                        rel + ": " + line)
            else:
                self.assertIsNone(pat.search(text), rel)

    def test_readme_makes_no_us_execution_claims(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = fh.read()
        pat = us_word_pattern()
        for line in readme.splitlines():
            if pat.search(line):
                self.assertTrue(self._us_context_ok(" ".join(line.lower().split())),
                                "README line looks like a US claim: " + line)

    def test_license_names_the_original_and_the_fork(self):
        with open(os.path.join(ROOT, "LICENSE"), encoding="utf-8") as fh:
            lic = fh.read()
        self.assertIn("MIT License", lic)
        self.assertIn("Copyright (c) 2026 Jake Schincariol", lic)
        self.assertIn("day-trader-skill-india", lic)
        self.assertIn("Jakeschincariol/day-trader-skill", lic)

    def test_every_tool_runs_on_python_3_8(self):
        # no 3.9+ only modules or syntax in the tools and the core (CI promises 3.8)
        folders = SKILLS + ["trader_core"]
        for folder in folders:
            for base, dirs, files in os.walk(os.path.join(ROOT, folder)):
                for f in files:
                    if not f.endswith(".py"):
                        continue
                    p = os.path.join(base, f)
                    with open(p, encoding="utf-8") as fh:
                        src = fh.read()
                    rel = os.path.relpath(p, ROOT)
                    for bad in ("import zoneinfo", "from zoneinfo", "import tomllib",
                                "from tomllib", ".removeprefix(", ".removesuffix("):
                        self.assertNotIn(bad, src, rel)
                    self.assertNotRegex(src, r"(?<![\w.])list\[", rel)
                    self.assertNotRegex(src, r"(?<![\w.])dict\[", rel)
                    self.assertNotRegex(src, r"(?<![\w.])tuple\[", rel)
                    self.assertNotRegex(src, r"(?m)^\s+match \w+.*:$", rel)

    def test_no_pycache_is_tracked(self):
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
        tracked = out.stdout.splitlines()
        self.assertTrue(tracked, "git ls-files returned nothing; run from a git checkout")
        hits = [f for f in tracked if "__pycache__" in f or f.endswith((".pyc", ".pyo"))]
        self.assertEqual(hits, [])

    def test_ci_runs_the_offline_suite(self):
        with open(os.path.join(ROOT, ".github", "workflows", "tests.yml"), encoding="utf-8") as fh:
            wf = fh.read()
        self.assertIn("python -m unittest discover -s tests", wf)
        for v in ("3.8", "3.9", "3.10", "3.11", "3.12", "3.13"):
            self.assertIn("'" + v + "'" if v in ("3.8", "3.13") else v, wf, v)
        self.assertIn("ubuntu-latest", wf)
        self.assertIn("windows-latest", wf)


if __name__ == "__main__":
    unittest.main()
