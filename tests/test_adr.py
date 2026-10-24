"""scripts/adr.py: ADRs are well formed, linked both ways, and cover every governed file.

The last section is the land gate for ADR coverage: it runs `adr.py check` over
this repository, so a governed file created by any route (a shell redirect gets
past the Write hook) fails the suite until an ADR names it.

Fixture expectations are literals (CLAUDE.md Rule 11).
"""
import os
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import adr  # noqa: E402
import harness_config  # noqa: E402

SCRATCH = Path(tempfile.mkdtemp(prefix="harness-adr."))
os.environ["HARNESS_EVENTS_LOG"] = str(SCRATCH / "events.jsonl")
ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
PASS, FAIL = [], []


def check(name, cond, detail: object = ""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("\n          " + str(detail)) if detail and not cond else ""))


def body(number="0001", title="Use a thing", status="accepted", components="hooks/a.py", extra="",
         sections=("Context", "Decision", "Consequences")):
    text = f"# {number}. {title}\n\nStatus: {status}\nDate: 2026-10-22\nComponents: {components}\n{extra}\n"
    return text + "".join(f"## {s}\n\nSome {s.lower()}.\n\n" for s in sections)


def problems_of(name, text):
    path = SCRATCH / "one" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)
    return adr.parse(path)[1]


# --- glob matching -------------------------------------------------------------
for pattern, path, expect in (
        (".claude/hooks/*.py", ".claude/hooks/x.py", True),
        (".claude/hooks/*.py", ".claude/hooks/lib/x.py", False),
        ("scripts/*", "scripts/land.sh", True),
        ("scripts/*", "scripts/sub/x.py", False),
        ("scripts/**", "scripts/sub/x.py", True),
        ("**/SKILL.md", ".claude/skills/land/SKILL.md", True),
        ("docs/adr/**", "docs/adr/0001-x.md", True),
        ("hooks/a.py", "hooks/a.py", True),
        ("hooks/a.py", "hooks/ab.py", False)):
    check(f"glob {pattern!r} {'matches' if expect else 'does not match'} {path!r}",
          adr.glob_match(pattern, path) == expect)

for pattern, path in ((".claude/hooks/*.py", ".Claude/HOOKS/Evil.PY"), (".claude/hooks/**", ".claude/hooks/lib/x.py")):
    check(f"glob {pattern!r} matches {path!r} (case-insensitive, subdirectories)", adr.glob_match(pattern, path))

# --- one record ------------------------------------------------------------------
check("a well-formed ADR has no problems", problems_of("0001-use-a-thing.md", body()) == [],
      problems_of("0001-use-a-thing.md", body()))
for name, text, expect in (
        ("0001-x.md", body(status="maybe"), "Status must be one of"),
        ("0001-x.md", body(sections=("Context", "Decision")), "missing section '## Consequences'"),
        ("0001-x.md", body().replace("Some decision.", ""), "section '## Decision' is empty"),
        ("1-x.md", body(), "file name must be NNNN-lowercase-slug.md"),
        ("0002-x.md", body(number="0001"), "heading number 0001 does not match the file name"),
        ("0001-x.md", "Status: accepted\n", "first line must be '# NNNN. Title'"),
        ("0001-x.md", body().replace("2026-10-22", "yesterday"), "Date must be YYYY-MM-DD"),
        ("0001-x.md", body(extra="Owner: me"), "unknown field 'Owner'"),
        ("0001-x.md", body(extra="Status: proposed"), "field Status given twice"),
        ("0001-x.md", body(components="../outside.py"), "must be a relative path inside the repository"),
        ("0001-x.md", body(status="superseded"), "must name its successor in Superseded-by"),
        ("0001-x.md", body(extra="Superseded-by: 0002"), "Superseded-by is set but Status is 'accepted'"),
        ("0001-x.md", body(extra="Supersedes: 7"), "Supersedes must list four-digit ADR numbers"),
        ("0001-x.md", body(components="**"), "component '**' must be a literal path, not a glob"),
        ("0001-x.md", body(components="scripts/*.py"), "component 'scripts/*.py' must be a literal path"),
        ("0001-x.md", body(components=".claude/**/[!z]*"), "must be a literal path"),
        ("0001-x.md", body(components="mutants/x?.py"), "must be a literal path")):
    found = problems_of(name, text)
    check(f"{name}: reported '{expect}'", any(expect in p for p in found), found)

# --- a directory: numbering and supersession links -------------------------------
d = SCRATCH / "dir"
d.mkdir()
(d / "README.md").write_text("not an ADR\n")
(d / "0001-old.md").write_text(body("0001", "Old", "superseded", extra="Superseded-by: 0002"))
(d / "0002-new.md").write_text(body("0002", "New", extra="Supersedes: 0001"))
records, problems = adr.load_all(d)
check("a README is not read as an ADR", len(records) == 2, [r["path"].name for r in records])
check("reciprocal supersession links pass", problems == [], problems)
(d / "0002-new.md").write_text(body("0002", "New"))
_, problems = adr.load_all(d)
check("a successor that does not list its predecessor is reported",
      "0001-old.md: 0002 does not list 0001 in Supersedes" in problems, problems)
(d / "0002-new.md").write_text(body("0002", "New", extra="Supersedes: 0009"))
_, problems = adr.load_all(d)
check("a Supersedes link to a missing ADR is reported", "0002-new.md: Supersedes 0009 does not exist" in problems,
      problems)
(d / "0002-dupe.md").write_text(body("0002", "Dupe"))
_, problems = adr.load_all(d)
check("two ADRs with one number are reported",
      any("number 0002 is also used by" in p for p in problems), problems)
check("next number follows the highest", adr.next_number(d) == "0003")
check("slugs are lowercase hyphenated", adr.slug("Lint, then Stop!") == "lint-then-stop")


# --- a repository: coverage and stale components -----------------------------------
def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True)


r = SCRATCH / "repo"
(r / "hooks").mkdir(parents=True)
(r / "docs" / "adr").mkdir(parents=True)
git(r, "init", "-q")
(r / "harness.toml").write_text('[adr]\nrequire_for = ["hooks/*.py"]\nexempt = ["hooks/scratch_1.py"]\n')
for name in ("a.py", "b.py", "scratch_1.py"):
    (r / "hooks" / name).write_text("x = 1\n")
(r / "docs" / "adr" / "0001-a.md").write_text(body("0001", "A", components="hooks/a.py"))
(r / "docs" / "adr" / "0002-b.md").write_text(body("0002", "B", "rejected", components="hooks/b.py"))
git(r, "add", "-A")
config = harness_config.load(r)
problems = adr.check_repo(r, config)
check("an uncovered governed file is reported with the command that fixes it",
      any(p.startswith("hooks/b.py: governed by adr.require_for") and "--component hooks/b.py" in p
          for p in problems), problems)
check("a rejected ADR does not cover its components", len([p for p in problems if "hooks/b.py" in p]) == 1, problems)
check("a covered file is not reported", not any(p.startswith("hooks/a.py") for p in problems), problems)
check("an exempt file is not reported", not any("scratch_1" in p for p in problems), problems)
check("the ADR directory itself is not governed", adr.governed(config, "docs/adr/0001-a.md") is False)
(r / "docs" / "adr" / "0002-b.md").write_text(body("0002", "B", "proposed", components="hooks/b.py"))
git(r, "rm", "-qf", "hooks/a.py")
problems = adr.check_repo(r, config)
check("an accepted ADR naming a deleted file is reported stale",
      any(p.startswith("0001-a.md: accepted ADR names 'hooks/a.py'") for p in problems), problems)
check("a proposed ADR may name a file that does not exist yet",
      not any(p.startswith("0002-b.md") for p in problems), problems)

done = subprocess.run([sys.executable, str(ROOT / "scripts" / "adr.py"), "new", "Add a hook",
                       "--component", "hooks/c.py"], cwd=r, capture_output=True, text=True, env=ENV)
created = r / done.stdout.strip()
check("adr new writes the next number as proposed",
      done.returncode == 0 and created.name == "0003-add-a-hook.md" and "Status: proposed" in created.read_text(),
      done.stdout + done.stderr)
check("an unedited adr new template is refused, section by section",
      adr.parse(created)[1] == ["0003-add-a-hook.md: section '## Context' still holds the template text",
                                "0003-add-a-hook.md: section '## Decision' still holds the template text",
                                "0003-add-a-hook.md: section '## Consequences' still holds the template text"],
      adr.parse(created)[1])

check("a project decision may govern no files",
      problems_of("0001-x.md", body(components="")) == [], problems_of("0001-x.md", body(components="")))

# --- the dated index and the change-set view ------------------------------------------
git(r, "add", "-A")
git(r, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-qm", "base")
# adr new stamps date.today(); pin it so the index expectation below stays a hand-written literal
created.write_text(created.read_text().replace(f"Date: {date.today().isoformat()}", "Date: 2026-10-22"))
records, _ = adr.load_all(r / "docs" / "adr")
adr.write_index(r / "docs" / "adr", records)
readme = (r / "docs" / "adr" / "README.md").read_text()
check("the index has one dated line per decision, linked to its file",
      "- 2026-10-22 — [0001. A](0001-a.md) — accepted" in readme
      and "- 2026-10-22 — [0003. Add a hook](0003-add-a-hook.md) — proposed" in readme, readme)
check("a fresh index passes check", not any("index is stale" in p for p in adr.check_repo(r, config)))
(r / "docs" / "adr" / "0004-later.md").write_text(body("0004", "Later", "proposed", components="hooks/b.py"))
check("an ADR added without regenerating the index is reported",
      "README.md: the ADR index is stale (python3 scripts/adr.py index)" in adr.check_repo(r, config))
git(r, "add", "-A")
git(r, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-qm", "later")
(r / "hooks" / "b.py").write_text("x = 2\n")
git(r, "add", "-A")
git(r, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-qm", "change b")
hits = {rec["number"]: paths for rec, paths in adr.touched(r.resolve(), config, "HEAD~1", "HEAD")}
check("touched names every ADR governing a changed file, and only those",
      hits == {"0002": ["hooks/b.py"], "0004": ["hooks/b.py"]}, hits)
hits = {rec["number"]: paths for rec, paths in adr.touched(r.resolve(), config, "HEAD~2", "HEAD~1")}
check("touched names an ADR whose own file changed", hits.get("0004", [None])[0] == "docs/adr/0004-later.md", hits)

# --- a repository adopting the harness starts with no ADR directory --------------------
fresh = SCRATCH / "fresh"
fresh.mkdir()
git(fresh, "init", "-q")
problems = adr.check_repo(fresh, harness_config.load(fresh))
check("a missing ADR directory fails check and names the command that creates it",
      problems == ["docs/adr/: the ADR directory does not exist, and adr.require_for governs files "
                   "(python3 scripts/adr.py init)"], problems)
done = subprocess.run([sys.executable, str(ROOT / "scripts" / "adr.py"), "init"], cwd=fresh,
                      capture_output=True, text=True, env=ENV)
check("adr init creates the directory and its README", done.returncode == 0
      and (fresh / "docs" / "adr" / "README.md").is_file(), done.stdout + done.stderr)
check("an initialised, empty repository passes check", adr.check_repo(fresh, harness_config.load(fresh)) == [],
      adr.check_repo(fresh, harness_config.load(fresh)))
(fresh / "docs" / "adr" / "README.md").write_text("# Mine\n")
subprocess.run([sys.executable, str(ROOT / "scripts" / "adr.py"), "init"], cwd=fresh, env=ENV, capture_output=True)
check("adr init never overwrites an existing README",
      (fresh / "docs" / "adr" / "README.md").read_text().startswith("# Mine\n"))
bare = SCRATCH / "bare"
bare.mkdir()
git(bare, "init", "-q")
subprocess.run([sys.executable, str(ROOT / "scripts" / "adr.py"), "new", "First", "--component", "x.py"],
               cwd=bare, env=ENV, capture_output=True)
check("adr new in a repository with no ADR directory creates it first",
      (bare / "docs" / "adr" / "README.md").is_file() and (bare / "docs" / "adr" / "0001-first.md").is_file())

(r / "docs" / "adr" / "README.md").write_text("# no markers\n")
check("a README with its index markers removed is reported, not skipped",
      "README.md: the ADR index markers are missing (python3 scripts/adr.py index)" in adr.check_repo(r, config))

# --- one record cannot cover a whole governed area (the round-3 reproduction) -------
w = SCRATCH / "wide"
(w / ".claude" / "hooks").mkdir(parents=True)
(w / "scripts").mkdir()
(w / "docs" / "adr").mkdir(parents=True)
git(w, "init", "-q")
(w / "harness.toml").write_text('[adr]\nrequire_for = [".claude/hooks/**", "scripts/**"]\n')
(w / ".claude" / "hooks" / "evil.py").write_text("x = 1\n")
(w / "scripts" / "evil.sh").write_text("true\n")
WIDE = (".claude/**", "scripts/**", ".claude/*/*", ".claude/**/[!z]*", "scripts/**/[!z]*", ".claude/hooks/*a*")
(w / "docs" / "adr" / "0001-everything.md").write_text(
    body("0001", "Everything", "proposed", components=", ".join(WIDE)))
records, _ = adr.load_all(w / "docs" / "adr")
adr.write_index(w / "docs" / "adr", records)
git(w, "add", "-A")
wide = harness_config.load(w)
problems = adr.check_repo(w, wide)
for c in WIDE:
    check(f"a glob component {c!r} is reported", any(p.startswith(f"0001-everything.md: component {c!r} must be a "
                                                                   "literal path") for p in problems), problems)
for f in (".claude/hooks/evil.py", "scripts/evil.sh"):
    check(f"{f} stays uncovered by glob components", any(p.startswith(f + ": governed") for p in problems), problems)
check("a glob component covers nothing for the Write gate either",
      adr.covering(records, ".claude/hooks/another.py") == [])
check("for and touched use the same rule: a glob ADR is listed for no path",
      adr.covering(records, ".claude/hooks/evil.py") == [])
(w / "docs" / "adr" / "0002-named.md").write_text(
    body("0002", "Named", "proposed", components=".claude/hooks/evil.py"))
records, _ = adr.load_all(w / "docs" / "adr")
check("a literal component covers its file, whatever the case of the path",
      [r["number"] for r in adr.covering(records, ".Claude/Hooks/EVIL.py")] == ["0002"])
(w / "docs" / "adr" / "README.md").unlink()
problems = adr.check_repo(w, wide)
check("deleting the ADR README is reported, not a way to switch the index check off",
      "README.md: the ADR index is missing (python3 scripts/adr.py init)" in problems, problems)

# --- this repository ---------------------------------------------------------------
# Rule 11: the governed set is pinned here, not read back from harness.toml. Each
# path must stay governed; narrowing adr.require_for to drop one fails this.
MUST_BE_GOVERNED = (".claude/hooks/adr_gate.py", ".claude/hooks/lib/helper.py", ".claude/agents/dual-derive.md",
                    ".claude/skills/land/SKILL.md", ".agents/skills/land/SKILL.md", ".claude/workflows/review-changes.js",
                    ".claude/harness/agent_harness.py", ".githooks/pre-commit", "scripts/land.sh",
                    "scripts/sub/tool.py", "mutants/runner.py")
repo_config = harness_config.load(ROOT)
for path in MUST_BE_GOVERNED:
    check(f"this repository governs {path}", adr.governed(repo_config, path))

done = subprocess.run([sys.executable, str(ROOT / "scripts" / "adr.py"), "check"], cwd=ROOT,
                      capture_output=True, text=True, env=ENV)
check("this repository's ADRs are well formed and cover every governed file", done.returncode == 0,
      done.stderr.strip())

print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
