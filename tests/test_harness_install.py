"""Exercise owned adoption, real entrypoints, upgrades and restoration in Git fixtures."""
import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from paths import REPO
import harness_install as install
from harness_lock import common_dir, git_environment


def main():
    """Run real local installations without modifying any global client state."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--proof", type=Path)
    args = parser.parse_args()
    passed, failed, commands = [], [], []
    directory = Path(tempfile.mkdtemp(prefix="owned-install-")).resolve()

    def check(name, condition, detail=""):
        (passed if condition else failed).append(name)
        print(f"{'PASS' if condition else 'FAIL'} {name}" + (f"\n{detail}" if detail and not condition else ""))

    def run(root, argv, payload=None, extra=None):
        result = subprocess.run(argv, cwd=root, env={**git_environment(), **(extra or {})},
                                input=payload, capture_output=True, text=True, timeout=45)
        commands.append(dict(cwd=str(root), argv=argv, returncode=result.returncode,
                             stdout=result.stdout, stderr=result.stderr))
        return result

    def git(root, *argv):
        result = run(root, ["git", "-C", str(root), *argv])
        if result.returncode:
            raise AssertionError(result.stderr)
        return result.stdout.strip()

    def write(root, name, text, mode=0o644):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)

    def repo(name):
        root = directory / name
        root.mkdir()
        git(root, "init", "-q", "-b", "work")
        for key, value in (("user.name", "fixture"), ("user.email", "fixture@example.invalid"),
                           ("commit.gpgsign", "false"), ("core.hooksPath", ".prior-hooks")):
            git(root, "config", "--local", key, value)
        return root

    def refused(operation, message):
        try:
            operation()
            return False
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            return message in str(error)

    source = repo("release")
    candidate = git(REPO, "rev-parse", "HEAD")
    release_names = (*install.PAYLOAD, *install.PROFILES, ".claude/settings.json", ".codex/hooks.json", ".codex/config.toml")
    for name in release_names:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if name in ("scripts/native_review.py", "scripts/harness_receipts.py"):
            path.write_bytes(install.blob(REPO, candidate, name))
        else:
            shutil.copy2(REPO / name, path)
    git(source, "add", "--all")
    git(source, "commit", "-qm", "release fixture")
    release_a = git(source, "rev-parse", "HEAD")
    target = repo("domain")
    protected = {
        "AGENTS.md": "Domain policy stays here.\n", "CLAUDE.md": "Domain instructions stay here.\n",
        "scripts/paths.py": "raise RuntimeError('domain paths must never be imported')\n",
        "scripts/gate.sh": "#!/bin/sh\nexit 66\n", "scripts/run.sh": "#!/bin/sh\nexit 67\n",
        ".codex/agents/custom.toml": 'name = "domain-agent"\n',
        "scripts/test_domain.sh": "#!/bin/sh\nprintf 'test\\n' >> steps\n[ ! -f fail-tests ] || exit 9\nprintf '2 checks total\\n'\n",
        ".prior-hooks/pre-commit": "#!/bin/sh\nprintf 'prior\\n' >> hook-log\n[ ! -f prior-fail ] || exit 17\n",
        ".prior-hooks/commit-msg": "#!/bin/sh\nprintf 'commit-msg %s\\n' \"$PWD\" >> prior-events\n[ ! -f reject-message ] || exit 19\n",
        ".prior-hooks/pre-push": "#!/bin/sh\nprintf 'pre-push %s %s %s\\n' \"$PWD\" \"$1\" \"$2\" >> prior-events\n[ ! -f reject-push ] || exit 23\n",
    }
    for name, text in protected.items():
        write(target, name, text, 0o755 if name.endswith(".sh") or name.startswith(".prior-hooks/") else 0o644)
    claude = {"permissions": {"deny": ["Bash(dangerous:*)"], "ask": ["Write(domain/*)"]},
              "env": {"DOMAIN_PRIVATE_SETTING": "fixture-private-value"}, "enabledPlugins": {"custom@domain": True},
              "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "true", "timeout": 3}]}]}}
    native = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "printf domain-native", "timeout": 2}]}]}}
    config = ('# Domain configuration\nmodel = "domain-model"\n'
              '[mcp_servers.smartsheet]\nurl = "https://mcp.smartsheet.com"\n'
              'enabled = false\nenabled_tools = ["get_sheet_version"] # narrower user policy\n'
              'credential_helper = "domain-helper"\n'
              '[mcp_servers.domain]\ncommand = "domain-server"\n'
              f'[apps.{install.APP}]\nenabled = true\n')
    write(target, ".claude/settings.json", json.dumps(claude, indent=2) + "\n", 0o600)
    write(target, ".codex/hooks.json", json.dumps(native, indent=2) + "\n", 0o640)
    write(target, ".codex/config.toml", config, 0o600)
    git(target, "add", "--all")
    git(target, "commit", "-qm", "domain base")
    (target / "subdir").mkdir()
    adoption = dict(setup="printf 'setup\\n' >> steps", lint="printf 'lint\\n' >> steps",
                    test="sh scripts/test_domain.sh", count_pattern=r"(\d+) checks total")

    def snapshot(root):
        return {str(path.relative_to(root)): (install.digest(path.read_bytes()), path.stat().st_mode & 0o777)
                for path in root.rglob("*") if path.is_file() and not path.is_symlink()}

    before = snapshot(target)
    value = install.plan(source, release_a, target, adoption)
    check("plan preserves every target byte, mode and Git file", snapshot(target) == before)
    check("dry run names concrete file and registration operations",
          all(item["operation"] == "CREATE" for item in value["files"]) and bool(value["registration_diff"])
          and value["git_config_diff"]["before"] == ".prior-hooks")
    check("plan retains no private target configuration values", "fixture-private-value" not in json.dumps(value)
          and "domain-helper" not in json.dumps(value))
    proof_a = install.apply(value)
    check("real apply verifies the adopted root", proof_a["governed_root"] == str(target))
    check("domain instructions, code and custom profiles stay byte-identical",
          all((target / name).read_text() == text for name, text in protected.items()))
    merged = json.loads((target / ".claude/settings.json").read_text())
    check("Claude deny, ask, env and plugins survive adoption",
          all(merged[key] == claude[key] for key in ("permissions", "env", "enabledPlugins")))
    policy = tomllib.loads((target / ".codex/config.toml").read_text())
    check("narrower disabled Smartsheet policy is never widened", policy["mcp_servers"]["smartsheet"]["enabled_tools"] == ["get_sheet_version"]
          and policy["mcp_servers"]["smartsheet"]["enabled"] is False)
    check("duplicate app is disabled while domain transport and helper survive", policy["apps"][install.APP]["enabled"] is False
          and policy["mcp_servers"]["domain"]["command"] == "domain-server"
          and policy["mcp_servers"]["smartsheet"]["credential_helper"] == "domain-helper")
    check("existing native config modes survive", (target / ".claude/settings.json").stat().st_mode & 0o777 == 0o600
          and (target / ".codex/hooks.json").stat().st_mode & 0o777 == 0o640
          and (target / ".codex/config.toml").stat().st_mode & 0o777 == 0o600)
    paths = run(target / "subdir", [sys.executable, str(target / ".vulfpeck/scripts/paths.py")])
    check("installed path authority resolves from a domain subdirectory", paths.returncode == 0 and paths.stdout.strip() == str(target))
    outside = run(directory, [sys.executable, str(target / ".vulfpeck/scripts/paths.py")])
    wrong_repo = run(source, [sys.executable, str(target / ".vulfpeck/scripts/paths.py")])
    check("installed path authority refuses non-Git and foreign-common execution", outside.returncode != 0 and wrong_repo.returncode != 0)
    gate = run(target / "subdir", [str(target / ".vulfpeck/scripts/gate.sh")])
    check("installed gate invokes explicit domain tests through its own runner", gate.returncode == 0
          and (target / "steps").read_text().splitlines() == ["setup", "lint", "test"], gate.stdout + gate.stderr)
    write(target, "fail-tests", "fail\n")
    gate = run(target, [str(target / ".vulfpeck/scripts/gate.sh")])
    check("installed gate propagates an actual domain test failure", gate.returncode == 9, gate.stdout + gate.stderr)
    relative_runner = run(target / "subdir", ["../.vulfpeck/scripts/run.sh", "--tests"])
    check("relative installed runner preserves the configured test failure after lock reexec", relative_runner.returncode == 9,
          relative_runner.stdout + relative_runner.stderr)
    (target / "fail-tests").unlink()
    relative_runner = run(target / "subdir", ["../.vulfpeck/scripts/run.sh", "--tests"])
    check("relative installed runner executes the configured domain tests", relative_runner.returncode == 0,
          relative_runner.stdout + relative_runner.stderr)
    # The mapped hook must use owned code even when the domain scripts are absent or hostile.
    write(target, "pkg/mod.py", "value = 1\n")
    write(target, "pkg/tests/test_mod.py", "print('owned-mapped-failure'); raise SystemExit(7)\n")
    mapped_cmd = next(hook["command"] for group in merged["hooks"]["PostToolUse"] for hook in group["hooks"]
                      if "test_for_edited_module.py" in hook["command"])
    mapped_event = dict(hook_event_name="PostToolUse", tool_name="Edit", tool_input={"file_path":str(target / "pkg/mod.py")},
                        cwd=str(target / "subdir"))
    mapped = run(target, ["sh", "-c", mapped_cmd], json.dumps(mapped_event), {"CLAUDE_PROJECT_DIR":str(target)})
    check("installed edit registration runs a failing mapped test without a domain reader", mapped.returncode == 2
          and "owned-mapped-failure" in mapped.stderr, mapped.stdout + mapped.stderr)
    write(target, "scripts/harness_config.py", "raise RuntimeError('domain config reader must never be imported')\n")
    mapped = run(target, ["sh", "-c", mapped_cmd], json.dumps(mapped_event), {"CLAUDE_PROJECT_DIR":str(target)})
    check("installed mapped test ignores the hostile domain config reader", mapped.returncode == 2
          and "owned-mapped-failure" in mapped.stderr and "domain config reader must never" not in mapped.stderr,
          mapped.stdout + mapped.stderr)
    (target / "scripts/harness_config.py").unlink()
    cmd = next(hook["command"] for group in merged["hooks"]["PreToolUse"] for hook in group["hooks"]
               if "require_agent_model.py" in hook["command"])
    event = dict(hook_event_name="PreToolUse", tool_name="Agent", tool_input={"subagent_type": "general-purpose"}, cwd=str(target))
    denied = run(target / "subdir", ["sh", "-c", cmd], json.dumps(event), {"CLAUDE_PROJECT_DIR": str(target)})
    check("actual installed Claude registration reaches the real model gate", denied.returncode == 0
          and json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny", denied.stderr)
    native_cmd = json.loads((target / ".codex/hooks.json").read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    event.update(tool_name="spawn_agent", tool_input={"agent_type": "unsupported"})
    denied = run(target / "subdir", ["sh", "-c", native_cmd], json.dumps(event))
    check("actual installed native registration reaches the real role gate", denied.returncode == 0
          and json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny", denied.stderr)
    linked = directory / "linked"
    git(target, "worktree", "add", "-q", "-b", "linked", str(linked))
    write(linked, ".prior-hooks/pre-push", protected[".prior-hooks/pre-push"].replace("exit 23", "exit 29"), 0o755)
    check("linked installation request refuses instead of rewriting primary", refused(lambda: install.plan(source, release_a, linked, adoption), "primary"))
    event["cwd"] = str(linked)
    denied = run(linked, ["sh", "-c", native_cmd], json.dumps(event))
    check("primary-discovered native hook executes against the linked checkout", denied.returncode == 0
          and json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny", denied.stderr)
    write(linked, "reject-message", "reject\n")
    linked_commit = run(linked, ["git", "-C", str(linked), "commit", "--allow-empty", "-qm", "linked refused"])
    check("linked Git discovery preserves its worktree-relative commit-msg refusal", linked_commit.returncode != 0
          and str(linked) in (linked / "prior-events").read_text(), linked_commit.stdout + linked_commit.stderr)
    (linked / "reject-message").unlink()
    linked_commit = run(linked, ["git", "-C", str(linked), "commit", "--allow-empty", "-qm", "linked accepted"])
    check("linked Git discovery reaches owned lint and the prior commit-msg hook", linked_commit.returncode == 0
          and (linked / "steps").is_file() and (linked / "steps").read_text().splitlines()[-1:] == ["lint"], linked_commit.stdout + linked_commit.stderr)
    write(linked, "reject-push", "reject\n")
    linked_push = run(linked, ["git", "-C", str(linked), "hook", "run", "pre-push", "--", "domain", "https://example.invalid"])
    check("linked Git preserves its distinct relative pre-push arguments and refusal status", linked_push.returncode == 29
          and f"pre-push {linked} domain https://example.invalid" in (linked / "prior-events").read_text(), linked_push.stdout + linked_push.stderr)
    (linked / "reject-push").unlink()
    doctor = run(linked, [sys.executable, str(target / ".vulfpeck/scripts/harness_doctor.py"), "--runtime", "codex", "--json"])
    doctor_report = json.loads(doctor.stdout) if doctor.returncode == 0 else {}
    check("installed doctor proves linked Git resolves the registered primary dispatcher", doctor.returncode == 0
          and "core.hooksPath resolves owned primary dispatchers from this checkout" in doctor_report.get("passes", []), doctor.stdout + doctor.stderr)
    gate = run(linked, [str(target / ".vulfpeck/scripts/gate.sh")])
    check("linked gate uses owned code with checkout-local test execution", gate.returncode == 0
          and (linked / "steps").read_text().splitlines()[-3:] == ["setup", "lint", "test"], gate.stdout + gate.stderr)
    write(linked, "harness.toml", '[commands]\ntest = "false"\n')
    gate = run(linked, [str(target / ".vulfpeck/scripts/gate.sh")])
    check("conflicting checkout-owned command policy is refused", gate.returncode != 0 and "conflicts" in gate.stderr, gate.stderr)
    (linked / "harness.toml").unlink()
    write(target, "prior-fail", "fail\n")
    commit = run(target, ["git", "-C", str(target), "commit", "--allow-empty", "-qm", "must refuse"])
    check("real Git dispatcher preserves the prior hook refusal status", commit.returncode != 0 and (target / "hook-log").exists())
    (target / "prior-fail").unlink()
    commit = run(target, ["git", "-C", str(target), "commit", "--allow-empty", "-qm", "owned hook fixture"])
    check("real Git dispatcher chains prior hook and installed lint", commit.returncode == 0, commit.stderr)
    write(target, "reject-message", "reject\n")
    refused_message = run(target, ["git", "-C", str(target), "commit", "--allow-empty", "-qm", "must reject message"])
    check("adoption preserves a prior commit-msg refusal", refused_message.returncode != 0, refused_message.stdout + refused_message.stderr)
    (target / "reject-message").unlink()
    edited = target / ".vulfpeck/scripts/paths.py"
    pristine = edited.read_bytes()
    edited.write_bytes(pristine + b"# user edit\n")
    check("upgrade refuses an edited owned file", refused(lambda: install.plan(source, release_a, target, adoption), "edited"))
    check("restore refuses an edited owned file", refused(lambda: install.restore(target), "edited"))
    edited.write_bytes(pristine)
    edited.chmod(0o600)
    check("upgrade refuses an edited owned mode", refused(lambda: install.plan(source, release_a, target, adoption), "edited"))
    check("owned mode edits also block restore", refused(lambda: install.restore(target), "edited"))
    edited.chmod(0o644)
    registration_path = target / ".codex/hooks.json"
    original_registration = registration_path.read_text()
    altered_registration = json.loads(original_registration)
    altered_registration["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] += 1
    registration_path.write_text(json.dumps(altered_registration))
    check("restore refuses an edited owned native registration", refused(lambda: install.restore(target), "registration edited"))
    registration_path.write_text(original_registration)
    # Foreign additions after adoption must survive both upgrade and restoration.
    merged["permissions"]["deny"].append("Write(later/*)")
    later_hook = {"hooks": [{"type": "command", "command": "printf later", "timeout": 1}]}
    merged["hooks"]["Stop"].append(later_hook)
    write(target, ".claude/settings.json", json.dumps(merged, indent=2) + "\n", 0o600)
    with (source / "scripts/harness_doctor.py").open("a") as stream:
        stream.write("\n# Release fixture B.\n")
    git(source, "add", "scripts/harness_doctor.py")
    git(source, "commit", "-qm", "release B")
    release_b = git(source, "rev-parse", "HEAD")
    upgrade = install.plan(source, release_b, target, adoption)
    proof_b = install.apply(upgrade)
    write(target, "reject-push", "reject\n")
    upgraded_push = run(target, ["git", "-C", str(target), "hook", "run", "pre-push", "--", "domain", "https://example.invalid"])
    check("upgrade preserves a prior pre-push refusal", upgraded_push.returncode == 23, upgraded_push.stdout + upgraded_push.stderr)
    (target / "reject-push").unlink()
    check("upgrade installs the exact committed next payload", install.digest((target / ".vulfpeck/scripts/harness_doctor.py").read_bytes())
          == install.digest(install.blob(source, release_b, "scripts/harness_doctor.py")))
    upgraded = json.loads((target / ".claude/settings.json").read_text())
    check("upgrade retains later permission and hook additions", upgraded["permissions"] == merged["permissions"] and later_hook in upgraded["hooks"]["Stop"])
    replan = install.plan(source, release_b, target, adoption)
    check("duplicate adoption has no payload or registration duplication", all(item["operation"] == "KEEP" for item in replan["files"])
          and len(upgraded["hooks"]["Stop"]) == len(merged["hooks"]["Stop"]))
    restore_b = install.restore(target)
    restored_b = install.restore(target, apply_changes=True, expected_plan=restore_b)
    check("restore recovers the prior owned generation", install.current_manifest(target)["revision"] == release_a
          and restored_b["verification"]["status"] == "VERIFIED_LOCAL_INSTALLATION")
    restored_hook = run(linked, ["git", "-C", str(linked), "hook", "run", "pre-push", "--", "restored", "https://example.invalid"])
    check("generation restore preserves linked prior Git hook discovery", restored_hook.returncode == 0
          and f"pre-push {linked} restored https://example.invalid" in (linked / "prior-events").read_text(), restored_hook.stderr)
    restore_a = install.restore(target)
    restored_a = install.restore(target, apply_changes=True, expected_plan=restore_a)
    final_claude = json.loads((target / ".claude/settings.json").read_text())
    expected_claude = copy.deepcopy(claude)
    expected_claude["permissions"]["deny"].append("Write(later/*)")
    expected_claude["hooks"]["Stop"].append(later_hook)
    check("final restore keeps foreign additions and removes owned registrations", final_claude == expected_claude
          and json.loads((target / ".codex/hooks.json").read_text()) == native)
    check("final restore recovers original policy bytes and Git hook path", (target / ".codex/config.toml").read_text() == config
          and install.local_hook_path(target) == ".prior-hooks")
    write(linked, "reject-message", "reject\n")
    final_hook = run(linked, ["git", "-C", str(linked), "commit", "--allow-empty", "-qm", "restore must reject"])
    check("final restore recovers worktree-relative prior commit-msg refusal", final_hook.returncode != 0, final_hook.stderr)
    (linked / "reject-message").unlink()
    check("restore removes only owned files and preserves protected domain bytes", not (target / ".vulfpeck/scripts/paths.py").exists()
          and all((target / name).read_text() == text for name, text in protected.items()))

    fresh = repo("fresh")
    git(fresh, "commit", "--allow-empty", "-qm", "base")
    git(fresh, "config", "core.hooksPath", ".vulfpeck/git-hooks")
    check("unowned reserved Git hook path refuses recursive dispatch", refused(lambda: install.plan(source, release_b, fresh, adoption), "would recurse"))
    git(fresh, "config", "core.hooksPath", ".prior-hooks")
    missing = install.plan(source, release_b, fresh, adoption)
    write(fresh, ".codex/agents/reviewer.toml", "user profile\n")
    check("stale destination preimage refuses before mutation", refused(lambda: install.apply(missing), "preimage changed"))
    collision = install.plan(source, release_b, fresh, adoption)
    check("unowned profile collision is concrete and refuses", bool(collision["conflicts"])
          and refused(lambda: install.apply(collision), "collisions"))
    (fresh / ".codex/agents/reviewer.toml").unlink()
    write(fresh, ".codex/config.toml", '[mcp_servers.smartsheet]\ncommand = "existing"\n')
    check("ambiguous native transport is refused without widening policy", refused(lambda: install.plan(source, release_b, fresh, adoption), "transport"))
    write(fresh, ".codex/config.toml", '["mcp_servers"."smartsheet"]\nurl = "https://mcp.smartsheet.com"\n')
    check("unsupported quoted policy layout refuses before any target mutation",
          refused(lambda: install.plan(source, release_b, fresh, adoption), "quoted policy table"))
    (fresh / ".codex/config.toml").unlink()
    foreign = directory / "foreign"
    foreign.mkdir()
    (fresh / ".vulfpeck").symlink_to(foreign, target_is_directory=True)
    check("linked payload destination is refused", refused(lambda: install.plan(source, release_b, fresh, adoption), "link"))
    (fresh / ".vulfpeck").unlink()
    git(fresh, "config", "extensions.worktreeConfig", "true")
    check("worktree-scoped hook migration requires separate review", refused(lambda: install.plan(source, release_b, fresh, adoption), "worktreeConfig"))
    git(fresh, "config", "--unset", "extensions.worktreeConfig")
    interrupted = install.plan(source, release_b, fresh, adoption)
    original_write = install.atomic_write
    calls = []

    def interrupt(root, path, content, mode=0o644):
        calls.append(str(path))
        if len(calls) == 4:
            raise OSError("controlled interrupted write")
        return original_write(root, path, content, mode)

    install.atomic_write = interrupt
    try:
        check("failed real apply reports the original write failure", refused(lambda: install.apply(interrupted), "interrupted write"))
    finally:
        install.atomic_write = original_write
    journal = json.loads((common_dir(fresh) / "harness/install" / interrupted["generation"] / "journal.json").read_text())
    check("interrupted apply journals and restores only applied owned steps", journal["status"] == "RESTORED"
          and not any((fresh / item["path"]).exists() for item in interrupted["files"])
          and install.local_hook_path(fresh) == ".prior-hooks")
    pending = install.plan(source, release_b, fresh, adoption)
    preexisting_plan = install.plan(source, release_b, fresh, adoption)
    calls.clear()
    def interrupt_process(root, path, content, mode=0o644):
        calls.append(str(path))
        result = original_write(root, path, content, mode)
        if len(calls) == 4:
            raise KeyboardInterrupt("controlled process interruption")
        return result
    install.atomic_write = interrupt_process
    try:
        try:
            install.apply(pending)
        except KeyboardInterrupt:
            pass
    finally:
        install.atomic_write = original_write
    check("process interruption blocks a later install until explicit restore",
          refused(lambda: install.plan(source, release_b, fresh, adoption), "incomplete"))
    check("an already planned install cannot cross an interrupted generation",
          refused(lambda: install.apply(preexisting_plan), "incomplete"))
    recovery = install.restore(fresh, generation=pending["generation"])
    install.restore(fresh, generation=pending["generation"], apply_changes=True, expected_plan=recovery)
    check("pre-write journal lets restore recover a post-write process interruption", install.incomplete(fresh) is None
          and not any((fresh / item["path"]).exists() for item in pending["files"]))
    verification_failure = install.plan(source, release_b, fresh, adoption)
    real_verify = install.verify
    install.verify = lambda root: (_ for _ in ()).throw(ValueError("controlled verification failure"))
    try:
        check("failed installed-entrypoint verification reports refusal",
              refused(lambda: install.apply(verification_failure), "controlled verification failure"))
    finally:
        install.verify = real_verify
    check("failed verification restores files, registrations and policy", install.current_manifest(fresh) is None
          and not (fresh / ".claude/settings.json").exists() and not (fresh / ".codex/config.toml").exists())
    cli = [sys.executable, str(REPO / "scripts/harness_install.py")]
    plan_path, receipt_path, restore_path = (directory / name for name in ("plan.json", "apply.json", "restore.json"))
    dry = run(fresh, [*cli, "plan", "--source", str(source), "--revision", release_b, "--target", str(fresh),
                      "--test", adoption["test"], "--output", str(plan_path)])
    applied = run(fresh, [*cli, "apply", "--plan", str(plan_path), "--output", str(receipt_path)])
    check("public CLI applies its actual pinned dry-run artifact", dry.returncode == 0 and applied.returncode == 0,
          dry.stderr + applied.stderr)
    planned_restore = run(fresh, [*cli, "restore", "--target", str(fresh), "--output", str(restore_path)])
    restored = run(fresh, [*cli, "restore", "--target", str(fresh), "--apply", "--plan", str(restore_path)])
    check("public CLI restores only its reviewed current preimages", planned_restore.returncode == 0 and restored.returncode == 0,
          planned_restore.stderr + restored.stderr)
    check("fresh adoption restores initially absent native configs", not (fresh / ".claude/settings.json").exists()
          and not (fresh / ".codex/hooks.json").exists() and not (fresh / ".codex/config.toml").exists())
    minimal = repo("existing-claude-only")
    settings = {"permissions": {"deny": ["Write(domain/*)"], "ask": ["Bash(domain:*)"]},
                "env": {"DOMAIN_SETTING": "fixture"}, "enabledPlugins": {"custom@domain": True}}
    write(minimal, ".claude/settings.json", json.dumps(settings) + "\n", 0o600)
    git(minimal, "add", "--all")
    git(minimal, "commit", "-qm", "domain settings")
    install.apply(install.plan(source, release_b, minimal,
                              dict(setup="", lint="", test="printf 'adopted domain command\\n'", count_pattern="")))
    minimal_gate = run(minimal, [str(minimal / ".vulfpeck/scripts/gate.sh")])
    minimal_restore = install.restore(minimal)
    install.restore(minimal, apply_changes=True, expected_plan=minimal_restore)
    check("Claude-only domain adoption runs its explicit command and restores exact settings semantics",
          minimal_gate.returncode == 0 and "adopted domain command" in minimal_gate.stdout
          and json.loads((minimal / ".claude/settings.json").read_text()) == settings
          and not (minimal / ".codex/config.toml").exists(), minimal_gate.stderr)
    artifact = dict(status="AUTHOR_CHECKS_PASS" if not failed else "AUTHOR_CHECKS_FAIL", fixture=str(directory),
                    candidate=candidate, release_a=release_a, release_b=release_b,
                    release_hashes={name: install.digest((source / name).read_bytes()) for name in release_names},
                    adoption=proof_a, upgrade=proof_b, restore_previous=restored_b, restore_original=restored_a,
                    checks=passed, failures=failed, commands=commands,
                    limits=["No global or actual-domain installation", "No native model turn or exact hook trust approval",
                            "No full gate or mutation corpus; parent owns independent verification"])
    if args.proof:
        install.save(args.proof, artifact)
    else:
        shutil.rmtree(directory)
    print(f"{len(passed)} passed, {len(failed)} failed")
    return bool(failed)


if __name__ == "__main__":
    raise SystemExit(main())
