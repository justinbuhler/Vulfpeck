"""Plan, apply, verify, and restore owned local harness installations."""
import argparse
import base64
import copy
import hashlib
import json
import os
import re
import secrets
import shlex
import subprocess
import tempfile
import tomllib
from pathlib import Path

from harness_lock import common_dir, git_environment, repository_lock

SCRIPTS = ("paths.py", "harness_config.py", "harness_env.sh", "gate.sh", "run.sh", "land.sh",
           "harness_events.py", "harness_doctor.py", "codegraph_freshness.py", "harness_lock.py",
           "harness_inputs.py", "harness_receipts.py", "adr.py", "native_hook_adapter.py", "native_review.py",
           "harness_install.py")
HOOKS = ("require_agent_model.py", "model_router.py", "blast_radius_advisory.py", "verify_commit_landed.py",
         "test_for_edited_module.py", "readonly_guard.py", "queue_guard.py", "session_guard.py", "adr_gate.py")
PAYLOAD = tuple(f"scripts/{name}" for name in SCRIPTS) + tuple(f".claude/hooks/{name}" for name in HOOKS) + (
    ".claude/harness/agent_harness.py",)
PROFILES = tuple(f".codex/agents/{name}.toml" for name in ("explorer", "executor", "reviewer", "planner"))
APP = "asdk_app_6a1ed9b99f888191aac2020e4cb301ff"
MANIFEST = ".vulfpeck/manifest.json"


def digest(data: bytes) -> str:
    """Hash exact file bytes."""
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> str:
    """Hash a deterministic nonsecret plan or registration."""
    return digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def git(root: Path, *args: str) -> str:
    """Run a checked local Git operation against an explicit root."""
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          env=git_environment(), check=True)
    return done.stdout.strip()


def blob(source: Path, revision: str, name: str) -> bytes:
    """Read only committed release content, never the author's working bytes."""
    done = subprocess.run(["git", "-C", str(source), "show", f"{revision}:{name}"],
                          capture_output=True, env=git_environment())
    if done.returncode:
        raise ValueError(f"release lacks required component {name}")
    return done.stdout


def target_root(path: Path) -> Path:
    """Refuse implicit redirection from a linked checkout to its primary."""
    root = path.resolve()
    if git(root, "rev-parse", "--show-toplevel") != str(root):
        raise ValueError("target must be the explicit Git top-level directory")
    if git(root, "config", "--get", "core.bare") == "true":
        raise ValueError("bare repository cannot be an installation target")
    worktree_config = subprocess.run(["git", "-C", str(root), "config", "--get", "extensions.worktreeConfig"],
                                    capture_output=True, text=True, env=git_environment())
    if worktree_config.stdout.strip() == "true":
        raise ValueError("worktreeConfig requires a separately reviewed hook-scope migration")
    primary = git(root, "worktree", "list", "--porcelain").splitlines()[0].removeprefix("worktree ")
    if Path(primary).resolve() != root:
        raise ValueError(f"linked target discovers primary hook declarations at {primary}; target primary explicitly")
    return root


def file_hash(path: Path) -> str | None:
    """Identify an existing regular destination without following links."""
    if path.is_symlink():
        raise ValueError(f"linked installation destination: {path}")
    if path.exists() and not path.is_file():
        raise ValueError(f"installation destination is not a file: {path}")
    return digest(path.read_bytes()) if path.exists() else None


def destination(root: Path, relative: str) -> Path:
    """Keep owned writes inside their literal target, excluding linked parents."""
    root = root.resolve()
    path = root / relative
    if not path.resolve().is_relative_to(root) or any(parent.is_symlink() for parent in [path, *path.parents] if parent != root):
        raise ValueError(f"installation path escapes or crosses a link: {relative}")
    return path


def current_manifest(root: Path) -> dict | None:
    """Require an installed manifest to match its registered local generation."""
    path = destination(root, MANIFEST)
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    if value.get("schema_version") != 1 or not re.fullmatch(r"[0-9a-f]{32}", value.get("generation", "")):
        raise ValueError("unknown installation manifest")
    registered = common_dir(root) / "harness/install" / value["generation"] / "manifest.json"
    if not registered.is_file() or registered.read_bytes() != path.read_bytes():
        raise ValueError("installation manifest was edited or is unregistered")
    if value["target_common"] != str(common_dir(root)):
        raise ValueError("installed target Git identity changed")
    return value


def registrations(source: Path, revision: str) -> dict:
    """Render owned registration paths while preserving native definition shapes."""
    result = {}
    for name in (".claude/settings.json", ".codex/hooks.json"):
        document = json.loads(blob(source, revision, name))
        entries = []
        for event, groups in document["hooks"].items():
            for index, original in enumerate(groups):
                group = copy.deepcopy(original)
                for hook in group["hooks"]:
                    command = hook["command"]
                    command = command.replace('${CLAUDE_PROJECT_DIR}/.claude/', '$harness_primary/.vulfpeck/.claude/')
                    command = command.replace('${CLAUDE_PROJECT_DIR}/scripts/', '$harness_primary/.vulfpeck/scripts/')
                    command = command.replace('python3 scripts/codegraph_freshness.py',
                                              'python3 "$harness_primary/.vulfpeck/scripts/codegraph_freshness.py"')
                    command = command.replace('codegraph_freshness.py"', 'codegraph_freshness.py" --root "$harness_checkout"')
                    command = command.replace('$root/scripts/', '$harness_primary/.vulfpeck/scripts/')
                    hook["command"] = ('harness_checkout=$(git rev-parse --show-toplevel) && '
                                       'harness_primary=$(git worktree list --porcelain | sed -n \'1s/^worktree //p\')'
                                       ' && test -n "$harness_primary" && '
                                       'python3 "$harness_primary/.vulfpeck/scripts/harness_install.py" verify --target "$harness_primary" >/dev/null && ' + command)
                entries.append({"id": f"{name}:{event}:{index}", "event": event, "entry": group})
        result[name] = entries
    return result


def merge_registrations(text: str, previous: list[dict], wanted: list[dict]) -> str:
    """Replace only exact owned entries, retaining every foreign property."""
    document = json.loads(text) if text else {}
    hooks = document.setdefault("hooks", {})
    for record in previous:
        entries = hooks.get(record["event"], [])
        matches = [index for index, entry in enumerate(entries) if entry == record["entry"]]
        if len(matches) != 1:
            raise ValueError(f"owned registration edited or missing: {record['id']}")
        entries.pop(matches[0])
        if not entries:
            hooks.pop(record["event"], None)
    for record in wanted:
        entries = hooks.setdefault(record["event"], [])
        if record["entry"] in entries:
            raise ValueError(f"unowned registration collision: {record['id']}")
        entries.append(record["entry"])
    return json.dumps(document, indent=2) + "\n"


def policy_edit(text: str, table: str, key: str, replacement: str | None) -> tuple[str, str | None]:
    """Edit one supported literal policy key without rewriting unrelated TOML."""
    parsed = tomllib.loads(text)
    body = parsed
    table_exists = True
    for part in table.split("."):
        table_exists = table_exists and part in body
        body = body.get(part, {})
        if not isinstance(body, dict):
            raise ValueError(f"policy table collision: {table}")
    header = re.search(r"(?m)^\[" + re.escape(table) + r"\][ \t]*(?:#[^\n]*)?\n?", text)
    if header is None:
        if table_exists:
            raise ValueError(f"unsupported quoted policy table: {table}")
        if replacement is None:
            return text, None
        return text + ("\n" if text and not text.endswith("\n\n") else "") + f"[{table}]\n{replacement}", None
    following = re.search(r"(?m)^\[", text[header.end():])
    end = header.end() + following.start() if following else len(text)
    section = text[header.end():end]
    match = re.search(r"(?m)^" + re.escape(key) + r"[ \t]*=[ \t]*(?:\[[\s\S]*?\]|[^\n]*)[^\n]*(?:\n|$)", section)
    if match is None:
        if key in body:
            raise ValueError(f"unsupported policy key layout: {table}.{key}")
        return text[:end] + (replacement or "") + text[end:], None
    before = match.group()
    start, stop = header.end() + match.start(), header.end() + match.end()
    return text[:start] + (replacement or "") + text[stop:], before


def restore_policy(text: str, changes: list[dict]) -> str:
    """Restore exact nonsecret owned spans, refusing edited policy."""
    for item in reversed(changes):
        text, current = policy_edit(text, item["table"], item["key"], item["before"])
        if current != item["after"]:
            raise ValueError(f"owned policy edited: {item['table']}.{item['key']}")
        if not item.get("table_existed", True):
            text = re.sub(r"(?m)^\[" + re.escape(item["table"]) + r"\][ \t]*\n(?=\s*(?:\[|\Z))", "", text)
    return text


def restricted_policy(text: str, source_text: str) -> tuple[str, list[dict]]:
    """Intersect tool limits and disable the duplicate app without copying auth."""
    source, target = tomllib.loads(source_text), tomllib.loads(text)
    allowed = source["mcp_servers"]["smartsheet"]["enabled_tools"]
    current = target.get("mcp_servers", {}).get("smartsheet", {})
    if "command" in current:
        raise ValueError("existing Smartsheet command transport conflicts with the release HTTP policy")
    if "enabled_tools" in current:
        if not isinstance(current["enabled_tools"], list) or not all(isinstance(value, str) for value in current["enabled_tools"]):
            raise ValueError("existing Smartsheet enabled_tools is not a string list")
        allowed = [value for value in current["enabled_tools"] if value in allowed]
    desired = [("mcp_servers.smartsheet", "enabled_tools", "enabled_tools = " + json.dumps(allowed) + "\n"),
               (f"apps.{APP}", "enabled", "enabled = false\n")]
    if "url" not in current:
        desired.insert(0, ("mcp_servers.smartsheet", "url", 'url = "https://mcp.smartsheet.com"\n'))
    changes = []
    for table, key, after in desired:
        table_existed = re.search(r"(?m)^\[" + re.escape(table) + r"\]", text) is not None
        text, before = policy_edit(text, table, key, after)
        changes.append(dict(table=table, key=key, before=before, after=after, table_existed=table_existed))
    tomllib.loads(text)
    return text, changes


def local_hook_path(root: Path) -> str | None:
    """Read only the repository-local hook override for reversible adoption."""
    result = subprocess.run(["git", "-C", str(root), "config", "--local", "--get", "core.hooksPath"],
                            env=git_environment(), capture_output=True, text=True)
    if result.returncode not in (0, 1):
        raise ValueError("cannot inspect local Git hook path")
    return result.stdout.strip() if result.returncode == 0 else None


def dispatcher(name: str, previous: str) -> bytes:
    """Chain the prior hook before the owned control, preserving its exit status."""
    prior = shlex.quote(str(Path(previous) / name))
    start = f'#!/bin/sh\nif [ -x {prior} ]; then\n  {prior} "$@" || exit $?\nfi\n'
    start += 'root=$(git rev-parse --show-toplevel) || exit 1\n'
    start += 'primary=$(git worktree list --porcelain | sed -n \'1s/^worktree //p\')\n[ -n "$primary" ] || exit 1\n'
    if name in ("pre-commit", "pre-merge-commit"):
        start += 'if [ "$(git symbolic-ref --quiet --short HEAD)" = main ]; then\n  echo "Use the owned lander for main" >&2; exit 1\nfi\n'
        if name == "pre-commit":
            start += 'unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_PREFIX GIT_COMMON_DIR GIT_NAMESPACE\nexec "$primary/.vulfpeck/scripts/gate.sh" --lint-only\n'
    else:
        start += '[ -d "$root/.codegraph" ] || exit 0\n'
        start += 'exec python3 "$primary/.vulfpeck/scripts/codegraph_freshness.py" --ensure --root "$root"\n'
    return start.encode()


def plan(source: Path, revision: str, target: Path, commands: dict) -> dict:
    """Produce exact owned changes and collisions without writing the target."""
    target = target_root(target)
    pending = incomplete(target)
    if pending:
        raise ValueError(f"incomplete installation {pending}; restore that --generation first")
    source = source.resolve()
    revision = git(source, "rev-parse", "--verify", revision + "^{commit}")
    previous = current_manifest(target)
    if (target / "harness.toml").exists():
        raise ValueError("target-owned harness.toml needs explicit reconciliation before namespaced adoption")
    if previous:
        verify(target)
    before_hook = local_hook_path(target)
    prior_directory = previous["prior_hook_directory"] if previous else git(target, "rev-parse", "--path-format=absolute", "--git-path", "hooks")
    if previous is None and Path(prior_directory).resolve() == target / ".vulfpeck/git-hooks":
        raise ValueError("unregistered owned Git hook path would recurse into its dispatcher")
    inherited = previous["initial_hook_path"] if previous else before_hook
    if previous and before_hook != ".vulfpeck/git-hooks":
        raise ValueError("owned Git hook path was changed")
    value = dict(schema_version=1, operation="install", source=str(source), revision=revision,
                 target=str(target), common=str(common_dir(target)), previous=previous,
                 generation=secrets.token_hex(16), files=[], conflicts=[], commands=commands,
                 prior_hook_directory=prior_directory, initial_hook_path=inherited, before_hook_path=before_hook)
    value["source_tree"] = git(source, "rev-parse", revision + "^{tree}")
    value["target_head"] = git(target, "rev-parse", "HEAD")
    value["target_status"] = git(target, "status", "--porcelain")
    value["git_config_diff"] = {"scope": "local", "key": "core.hooksPath", "before": before_hook,
                                "after": ".vulfpeck/git-hooks"}
    contents = {}
    for name in PAYLOAD:
        contents[".vulfpeck/" + name] = blob(source, revision, name)
    for name in PROFILES:
        contents[name] = blob(source, revision, name)
    config = "[commands]\n" + "".join(f"{key} = {json.dumps(commands[key])}\n" for key in ("setup", "lint", "test"))
    config += "\n[tests]\ncount_pattern = " + json.dumps(commands["count_pattern"]) + "\n"
    contents[".vulfpeck/harness.toml"] = config.encode()
    for name in ("pre-commit", "pre-merge-commit", "post-checkout", "post-merge"):
        contents[f".vulfpeck/git-hooks/{name}"] = dispatcher(name, prior_directory)
    if previous and set(previous["files"]) - set(contents):
        raise ValueError("release removes owned components; restore the previous installation before adopting it")
    for relative, content in contents.items():
        path = destination(target, relative)
        before = file_hash(path)
        owned = previous and relative in previous["files"]
        if before and not owned:
            value["conflicts"].append(f"unowned destination: {relative}")
        executable = relative.endswith(".sh") or relative.startswith(".vulfpeck/git-hooks/")
        after = digest(content)
        value["files"].append(dict(path=relative, before=before, after=after,
                                   operation="CONFLICT" if before and not owned else "KEEP" if before == after else "UPDATE" if before else "CREATE",
                                   before_mode=(path.stat().st_mode & 0o777) if path.exists() else None,
                                   mode=0o755 if executable else 0o644,
                                   content=base64.b64encode(content).decode()))
    value["registrations"] = registrations(source, revision)
    value["config_preimages"] = {}
    value["config_had_hooks"] = {}
    value["registration_diff"] = {}
    for relative, wanted in value["registrations"].items():
        path = destination(target, relative)
        old = path.read_text() if path.exists() else ""
        value["config_had_hooks"][relative] = "hooks" in json.loads(old or "{}")
        before_entries = previous["registrations"][relative] if previous else []
        after = merge_registrations(old, before_entries, wanted)
        value["config_preimages"][relative] = file_hash(path)
        value["registration_diff"][relative] = {"remove": before_entries, "add": wanted}
    policy = destination(target, ".codex/config.toml")
    old = policy.read_text() if policy.exists() else ""
    base = restore_policy(old, previous["policy"]) if previous else old
    after, changes = restricted_policy(base, blob(source, revision, ".codex/config.toml").decode())
    value["policy"] = changes
    value["policy_diff"] = changes
    value["config_preimages"][".codex/config.toml"] = file_hash(policy)
    value["initial_config_exists"] = previous["initial_config_exists"] if previous else {
        relative: destination(target, relative).exists() for relative in value["config_preimages"]}
    value["plan_sha256"] = canonical(value)
    return value


def atomic_write(root: Path, path: Path, data: bytes, mode: int = 0o644) -> None:
    """Replace a file through a private same-target temporary, without backups."""
    scratch = root / "tmp/txt"
    scratch.mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=scratch, prefix="install-", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def save(path: Path, value: dict) -> None:
    """Write a named nonsecret planning or journal artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".pending")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def incomplete(target: Path) -> str | None:
    """Find an interrupted owned transaction before another installation starts."""
    for path in (common_dir(target) / "harness/install").glob("*/journal.json"):
        journal = json.loads(path.read_text())
        if journal["status"] in ("APPLYING", "ROLLBACK_FAILED"):
            return journal["generation"]
    return None


def payload_from_generation(target: Path, generation: str) -> dict:
    """Read registered public payload bytes retained for an owned rollback."""
    journal = common_dir(target) / "harness/install" / generation / "journal.json"
    return {item["path"]: item for item in json.loads(journal.read_text())["plan"]["files"]}


def write_config(target: Path, relative: str, text: str, existed: bool) -> None:
    """Keep foreign config additions and remove only an empty owned-created file."""
    path = destination(target, relative)
    empty = not tomllib.loads(text) if relative.endswith(".toml") else not any(json.loads(text).values())
    if not existed and empty:
        path.unlink(missing_ok=True)
    else:
        atomic_write(target, path, text.encode(), path.stat().st_mode & 0o777 if path.exists() else 0o644)


def undo(target: Path, journal: dict, *, apply_changes: bool = True) -> dict:
    """Restore applied owned steps without retaining unrelated config snapshots."""
    value, previous = journal["plan"], journal["plan"]["previous"]
    completed = journal["completed"]
    old_files = payload_from_generation(target, previous["generation"]) if previous else {}
    # Validate every touched surface before changing any of them.
    for item in value["files"]:
        if item["path"] not in completed:
            continue
        path = destination(target, item["path"])
        current = file_hash(path)
        if current not in (item["before"], item["after"]):
            raise ValueError(f"owned file edited during restore: {item['path']}")
        if current == item["after"] and path.stat().st_mode & 0o777 != item["mode"]:
            raise ValueError(f"owned file mode edited during restore: {item['path']}")
    configs = {}
    for relative, entries in value["registrations"].items():
        if relative not in completed:
            continue
        path = destination(target, relative)
        if file_hash(path) == value["config_preimages"][relative]:
            continue
        text = merge_registrations(path.read_text(), entries, previous["registrations"][relative] if previous else [])
        document = json.loads(text)
        if not value["config_had_hooks"][relative] and not document["hooks"]:
            document.pop("hooks")
            text = json.dumps(document, indent=2) + "\n"
        configs[relative] = text
    relative = ".codex/config.toml"
    path = destination(target, relative)
    if relative in completed and file_hash(path) != value["config_preimages"][relative]:
        text = restore_policy(path.read_text(), value["policy"])
        if previous:
            for item in previous["policy"]:
                text, before = policy_edit(text, item["table"], item["key"], item["after"])
                if before != item["before"]:
                    raise ValueError("prior owned policy cannot be restored")
        configs[relative] = text
    hook_path = local_hook_path(target)
    if hook_path not in (value["before_hook_path"], ".vulfpeck/git-hooks"):
        raise ValueError("Git hook path edited during restore")
    result = dict(operation="restore", target=str(target), generation=value["generation"],
                  previous_generation=previous["generation"] if previous else None,
                  files={item["path"]: file_hash(destination(target, item["path"]))
                         for item in value["files"] if item["path"] in completed},
                  configs={relative: file_hash(destination(target, relative)) for relative in configs},
                  remove_registrations={name: entries for name, entries in value["registrations"].items() if name in completed},
                  restore_registrations=previous["registrations"] if previous else {},
                  restore_policy=value["policy"] if ".codex/config.toml" in completed else [],
                  restore_git_hook_path=value["before_hook_path"])
    result["plan_sha256"] = canonical(result)
    if not apply_changes:
        return result
    for relative, text in configs.items():
        write_config(target, relative, text, value["config_preimages"][relative] is not None)
    for item in reversed(value["files"]):
        if item["path"] not in completed:
            continue
        path = destination(target, item["path"])
        if item["before"] is None:
            path.unlink(missing_ok=True)
        else:
            prior = old_files[item["path"]]
            atomic_write(target, path, base64.b64decode(prior["content"], validate=True), prior["mode"])
    manifest_path = destination(target, MANIFEST)
    if previous:
        atomic_write(target, manifest_path, (json.dumps(previous, indent=2) + "\n").encode())
    else:
        manifest_path.unlink(missing_ok=True)
    if value["before_hook_path"] is None:
        if hook_path is not None:
            git(target, "config", "--local", "--unset", "core.hooksPath")
    else:
        git(target, "config", "--local", "core.hooksPath", value["before_hook_path"])
    for relative in configs:
        if previous and relative in previous["registrations"]:
            merge_registrations(destination(target, relative).read_text(), previous["registrations"][relative],
                                previous["registrations"][relative])
    proof = verify(target) if previous else {"status": "RESTORED_PREINSTALLATION", "governed_root": str(target)}
    journal.update(status="RESTORED", restore_verification=proof)
    save(common_dir(target) / "harness/install" / value["generation"] / "journal.json", journal)
    return {**result, "status": "RESTORED", "verification": proof}


def verify(target: Path) -> dict:
    """Check actual installed files, registrations, policy and root resolution."""
    target = target.resolve()
    manifest = current_manifest(target)
    if manifest is None:
        raise ValueError("target has no registered installation")
    for relative, expected in manifest["files"].items():
        path = destination(target, relative)
        if file_hash(path) != expected or path.stat().st_mode & 0o777 != manifest["file_modes"][relative]:
            raise ValueError(f"owned file edited or missing: {relative}")
    for relative, entries in manifest["registrations"].items():
        merge_registrations(destination(target, relative).read_text(), entries, entries)
    restore_policy(destination(target, ".codex/config.toml").read_text(), manifest["policy"])
    if local_hook_path(target) != ".vulfpeck/git-hooks":
        raise ValueError("owned Git hook path changed")
    resolved = subprocess.check_output(["python3", str(target / ".vulfpeck/scripts/paths.py")], cwd=target,
                                       env=git_environment(), text=True).strip()
    if Path(resolved).resolve() != target:
        raise ValueError("installed paths.REPO does not resolve the governed target")
    return {"status": "VERIFIED_LOCAL_INSTALLATION", "target": str(target), "generation": manifest["generation"],
            "native_trust": "not approved or proved by installer", "governed_root": resolved}


def _apply_steps(value: dict) -> dict:
    """Apply a reviewed plan after rechecking all exact preimages."""
    supplied = value["plan_sha256"]
    if canonical({key: item for key, item in value.items() if key != "plan_sha256"}) != supplied:
        raise ValueError("installation plan was changed")
    target = target_root(Path(value["target"]))
    if incomplete(target):
        raise ValueError("an incomplete installation requires restoration")
    source = Path(value["source"])
    if git(source, "rev-parse", value["revision"] + "^{tree}") != value["source_tree"]:
        raise ValueError("committed source tree changed")
    release = {".vulfpeck/" + name: blob(source, value["revision"], name) for name in PAYLOAD}
    release.update({name: blob(source, value["revision"], name) for name in PROFILES})
    if any(digest(release[item["path"]]) != item["after"] for item in value["files"] if item["path"] in release):
        raise ValueError("plan payload differs from committed release")
    if registrations(source, value["revision"]) != value["registrations"]:
        raise ValueError("plan registrations differ from committed release")
    if value["conflicts"]:
        raise ValueError("installation collisions: " + "; ".join(value["conflicts"]))
    if str(common_dir(target)) != value["common"] or current_manifest(target) != value["previous"]:
        raise ValueError("installation target/generation moved since plan")
    if git(target, "rev-parse", "HEAD") != value["target_head"]:
        raise ValueError("target HEAD moved since plan")
    for item in value["files"]:
        path = destination(target, item["path"])
        if (file_hash(path) != item["before"] or
                (path.exists() and path.stat().st_mode & 0o777 != item["before_mode"])):
            raise ValueError(f"file preimage changed: {item['path']}")
    for relative, before in value["config_preimages"].items():
        if file_hash(destination(target, relative)) != before:
            raise ValueError(f"configuration preimage changed: {relative}")
    if local_hook_path(target) != value["before_hook_path"]:
        raise ValueError("Git hook path moved since plan")
    directory = common_dir(target) / "harness/install" / value["generation"]
    if directory.exists():
        raise ValueError("installation generation was already used; create a new plan")
    journal = {"status": "APPLYING", "generation": value["generation"], "completed": [], "plan": value}
    save(directory / "journal.json", journal)
    for item in value["files"]:
        content = base64.b64decode(item["content"], validate=True)
        if digest(content) != item["after"]:
            raise ValueError("payload digest does not match plan")
        journal["completed"].append(item["path"])
        save(directory / "journal.json", journal)
        atomic_write(target, destination(target, item["path"]), content, item["mode"])
    previous = value["previous"]
    for relative, wanted in value["registrations"].items():
        path = destination(target, relative)
        old = path.read_text() if path.exists() else ""
        text = merge_registrations(old, previous["registrations"][relative] if previous else [], wanted)
        journal["completed"].append(relative)
        save(directory / "journal.json", journal)
        atomic_write(target, path, text.encode(), path.stat().st_mode & 0o777 if path.exists() else 0o644)
    path = destination(target, ".codex/config.toml")
    text = path.read_text() if path.exists() else ""
    if previous:
        text = restore_policy(text, previous["policy"])
    for item in value["policy"]:
        text, before = policy_edit(text, item["table"], item["key"], item["after"])
        if before != item["before"]:
            raise ValueError("policy preimage changed")
    journal["completed"].append(".codex/config.toml")
    save(directory / "journal.json", journal)
    atomic_write(target, path, text.encode(), path.stat().st_mode & 0o777 if path.exists() else 0o644)
    manifest = {key: value[key] for key in ("schema_version", "generation", "source", "revision", "commands",
                "registrations", "policy", "prior_hook_directory", "initial_hook_path", "initial_config_exists")}
    manifest["files"] = {item["path"]: item["after"] for item in value["files"]}
    manifest["file_modes"] = {item["path"]: item["mode"] for item in value["files"]}
    manifest["registration_hashes"] = {name: {item["id"]: canonical(item["entry"]) for item in entries}
                                       for name, entries in value["registrations"].items()}
    manifest["target_common"] = value["common"]
    manifest["source_tree"] = value["source_tree"]
    manifest["previous_generation"] = previous["generation"] if previous else None
    save(directory / "manifest.json", manifest)
    atomic_write(target, destination(target, MANIFEST), (json.dumps(manifest, indent=2) + "\n").encode())
    git(target, "config", "--local", "core.hooksPath", ".vulfpeck/git-hooks")
    proof = verify(target)
    journal.update(status="COMPLETE", verification=proof)
    save(directory / "journal.json", journal)
    return proof


def apply(value: dict) -> dict:
    """Apply a plan and restore failed owned steps under the same mutexes."""
    target = target_root(Path(value["target"]))
    # Keep verification ownership through both the mutation and any rollback.
    with repository_lock(target, "verification"), repository_lock(target, "installation"):
        try:
            return _apply_steps(value)
        except (ValueError, OSError, KeyError, subprocess.SubprocessError) as failure:
            path = common_dir(target) / "harness/install" / value["generation"] / "journal.json"
            if path.exists():
                journal = json.loads(path.read_text())
                if journal["status"] == "APPLYING":
                    try:
                        undo(target, journal)
                    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as rollback:
                        journal.update(status="ROLLBACK_FAILED", failure=str(failure), restore_failure=str(rollback))
                        save(path, journal)
                        raise ValueError(f"installation failed: {failure}; restore --generation {value['generation']}: {rollback}") from failure
            raise


def restore(target: Path, *, apply_changes: bool = False, generation: str | None = None,
            expected_plan: dict | None = None) -> dict:
    """Plan or restore one owned generation while retaining foreign additions."""
    target = target_root(target)
    with repository_lock(target, "verification"), repository_lock(target, "installation"):
        manifest = current_manifest(target)
        generation = generation or (manifest["generation"] if manifest else incomplete(target))
        if not generation or not re.fullmatch(r"[0-9a-f]{32}", generation):
            raise ValueError("no owned generation to restore")
        path = common_dir(target) / "harness/install" / generation / "journal.json"
        journal = json.loads(path.read_text())
        if journal["status"] == "COMPLETE":
            if manifest is None or manifest["generation"] != generation:
                raise ValueError("only the current installed generation can be restored")
            verify(target)
        elif journal["status"] not in ("APPLYING", "ROLLBACK_FAILED"):
            raise ValueError("generation was already restored")
        result = undo(target, journal, apply_changes=False)
        if expected_plan is not None and result != expected_plan:
            raise ValueError("restore preimages changed; create a new restore plan")
        return undo(target, journal) if apply_changes else result


def main() -> int:
    """Run explicit local plan/apply/verify/restore commands."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("plan", "apply", "verify", "restore"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--target", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--setup", default="")
    parser.add_argument("--lint", default="")
    parser.add_argument("--test")
    parser.add_argument("--count-pattern", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--generation")
    args = parser.parse_args()
    try:
        if args.operation == "plan":
            if args.source is None or args.target is None or args.test is None:
                parser.error("plan requires --source, --target and explicit --test")
            result = plan(args.source, args.revision, args.target,
                          dict(setup=args.setup, lint=args.lint, test=args.test, count_pattern=args.count_pattern))
        elif args.operation == "apply":
            if args.plan is None:
                parser.error("apply requires --plan")
            result = apply(json.loads(args.plan.read_text()))
        elif args.operation == "verify":
            if args.target is None:
                parser.error("verify requires --target")
            result = verify(args.target)
        else:
            if args.target is None or (args.apply and args.plan is None):
                parser.error("restore requires --target; --apply also requires a reviewed --plan")
            result = restore(args.target, apply_changes=args.apply, generation=args.generation,
                             expected_plan=json.loads(args.plan.read_text()) if args.plan else None)
        if args.output:
            save(args.output, result)
        else:
            print(json.dumps(result, indent=2))
        return 0
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"installer: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
