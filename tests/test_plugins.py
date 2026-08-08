import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sasori import (  # noqa: E402
    FULL_HOST_PROCESS_PRIVILEGES,
    Harness,
    Message,
    ModelReply,
    PluginRegistration,
    RunPaused,
    SQLiteStore,
    SkillSpec,
    Tool,
    ToolCall,
    WorkerSpec,
)
from sasori.plugins import (  # noqa: E402
    ManifestDependency,
    ManifestError,
    ManifestSkill,
    ManifestTool,
    ManifestWorker,
    PermissionSet,
    PluginLoadError,
    PluginPermissionError,
    RegistrationError,
    disclose_permissions,
    discover_plugins,
    load_enabled_plugins,
    manifest_upgrade_diff,
    parse_manifest,
    skill_content_sha256,
    validate_registration,
    worker_content_sha256,
)
from sasori_plugins.workspace import (  # noqa: E402
    WorkspaceFileError,
    WorkspaceLimitError,
    WorkspaceLimits,
    WorkspacePathError,
    workspace_manifest,
    workspace_registration,
)


def manifest_data():
    data = json.loads(json.dumps(asdict(workspace_manifest())))
    data["permissions"].pop("full_host")
    return data


def manifest_json(data=None):
    return json.dumps(data or manifest_data(), ensure_ascii=True)


def workspace_grant():
    return workspace_manifest().permissions


class FakeDistribution:
    def __init__(self, name="sasori", version="0.1.0.dev0"):
        self.name = name
        self.version = version


class FakeEntryPoint:
    group = "sasori.plugins"

    def __init__(
        self,
        factory,
        *,
        name="com.sasori.workspace",
        value="sasori_plugins.workspace:register",
        distribution="sasori",
        version="0.1.0.dev0",
    ):
        self.name = name
        self.value = value
        self.dist = FakeDistribution(distribution, version)
        self.factory = factory
        self.load_count = 0

    def load(self):
        self.load_count += 1
        return self.factory


class ScriptedModel:
    def __init__(self, *replies):
        self.replies = list(replies)

    async def complete(self, messages, tools):
        reply = self.replies.pop(0)
        return reply(messages) if callable(reply) else reply


class ManifestTests(unittest.TestCase):
    def test_valid_manifest_round_trips(self):
        manifest = parse_manifest(manifest_json())
        self.assertEqual(manifest.plugin_id, "com.sasori.workspace")
        self.assertEqual([tool.name for tool in manifest.tools], [
            "list_files",
            "read_text",
            "search_text",
            "write_text",
        ])

    def test_json_is_strict_and_bounded(self):
        raw = manifest_json()
        duplicate = raw.replace(
            '"schema_version": 1,',
            '"schema_version": 1, "schema_version": 1,',
            1,
        )
        invalid = [
            duplicate,
            raw.replace('"schema_version": 1', '"schema_version": NaN', 1),
            raw.replace('"schema_version": 1', '"schema_version": Infinity', 1),
            raw.replace('"schema_version": 1', '"schema_version": 1e9999', 1),
            b"\xff",
            b" " * (256 * 1024 + 1),
        ]
        for value in invalid:
            with self.subTest(value=str(value)[:40]):
                with self.assertRaises(ManifestError):
                    parse_manifest(value)

    def test_unknown_fields_and_schema_versions_are_rejected(self):
        cases = []
        top = manifest_data()
        top["unknown"] = True
        cases.append(top)
        nested = manifest_data()
        nested["tools"][0]["unknown"] = True
        cases.append(nested)
        execution = manifest_data()
        execution["execution"]["unknown"] = True
        cases.append(execution)
        for version in (True, 0, 2):
            data = manifest_data()
            data["schema_version"] = version
            cases.append(data)
        for data in cases:
            with self.subTest(data=data):
                with self.assertRaises(ManifestError):
                    parse_manifest(manifest_json(data))

    def test_invalid_ids_and_duplicate_contributions_are_rejected(self):
        cases = []
        plugin = manifest_data()
        plugin["plugin_id"] = "Bad Plugin"
        cases.append(plugin)
        tool = manifest_data()
        tool["tools"][0]["name"] = "bad.tool"
        cases.append(tool)
        skill = manifest_data()
        skill["skills"] = [{
            "skill_id": "someone.else/read",
            "version": "1",
            "content_sha256": "0" * 64,
        }]
        cases.append(skill)
        worker = manifest_data()
        worker["workers"] = [{
            "worker_id": "someone.else/reader",
            "version": "1",
            "content_sha256": "0" * 64,
        }]
        cases.append(worker)

        duplicate_tool = manifest_data()
        duplicate_tool["tools"].append(copy.deepcopy(duplicate_tool["tools"][0]))
        cases.append(duplicate_tool)
        duplicate_skill = manifest_data()
        duplicate_skill["skills"] = [
            {
                "skill_id": "com.sasori.workspace/read",
                "version": "1",
                "content_sha256": "0" * 64,
            }
        ] * 2
        cases.append(duplicate_skill)
        duplicate_worker = manifest_data()
        duplicate_worker["workers"] = [
            {
                "worker_id": "com.sasori.workspace/reader",
                "version": "1",
                "content_sha256": "0" * 64,
            }
        ] * 2
        cases.append(duplicate_worker)
        duplicate_dependency = manifest_data()
        dependency = {
            "name": "example",
            "version": "1",
            "filename": "example.whl",
            "sha256": "0" * 64,
        }
        duplicate_dependency["dependencies"] = [dependency, copy.deepcopy(dependency)]
        cases.append(duplicate_dependency)

        for data in cases:
            with self.subTest(data=data):
                with self.assertRaises(ManifestError):
                    parse_manifest(manifest_json(data))

    def test_text_and_logical_filesystem_capabilities_are_strict(self):
        cases = []
        oversized = manifest_data()
        oversized["summary"] = "x" * 4097
        cases.append(oversized)
        controlled = manifest_data()
        controlled["summary"] = "line\nbreak"
        cases.append(controlled)
        surrogate = manifest_data()
        surrogate["name"] = "\ud800"
        cases.append(surrogate)
        for capability in (
            "C:/Windows",
            "workspace:/etc",
            "workspace:../outside",
            "workspace:C:\\Windows",
            "workspace:a//b",
        ):
            data = manifest_data()
            data["permissions"]["filesystem_read"] = [capability]
            cases.append(data)
        for data in cases:
            with self.subTest(data=data):
                with self.assertRaises(ManifestError):
                    parse_manifest(manifest_json(data))


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manifest = workspace_manifest()
        self.registration = workspace_registration(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def contribution_fixture(self):
        skill = SkillSpec(
            "com.sasori.workspace/read",
            "1",
            "Read workspace",
            "Read a text file.",
            "Use read_text for bounded UTF-8 files.",
            ("read_text",),
            skill_content_sha256("Use read_text for bounded UTF-8 files."),
        )
        worker = WorkerSpec(
            "com.sasori.workspace/reader",
            "1",
            "Workspace reader",
            "Reads a requested file.",
            "Read only the requested workspace text.",
            "default",
            (skill.skill_id,),
            ("read_text",),
            4,
            30.0,
            10.0,
            ("read_only",),
        )
        worker = replace(worker, content_sha256=worker_content_sha256(worker))
        manifest = replace(
            self.manifest,
            skills=(ManifestSkill(skill.skill_id, skill.version, skill.content_sha256),),
            workers=(
                ManifestWorker(worker.worker_id, worker.version, worker.content_sha256),
            ),
        )
        registration = replace(
            self.registration,
            skills=(skill,),
            workers=(worker,),
        )
        return manifest, registration, skill, worker

    def test_workspace_registration_matches_manifest(self):
        self.assertIs(
            validate_registration(self.manifest, self.registration),
            self.registration,
        )

    def test_registration_identity_and_tool_sets_must_match(self):
        cases = [
            replace(self.registration, api_version=2),
            replace(self.registration, plugin_id="com.example.other"),
            replace(self.registration, version="9"),
            replace(self.registration, tools=self.registration.tools[:-1]),
            replace(
                self.registration,
                tools=self.registration.tools
                + (Tool("extra", lambda: None, effect="read_only"),),
            ),
        ]
        for registration in cases:
            with self.subTest(registration=registration):
                with self.assertRaises(RegistrationError):
                    validate_registration(self.manifest, registration)

    def test_tool_effect_revision_and_schema_must_match(self):
        tools = {tool.name: tool for tool in self.registration.tools}
        cases = []
        changed_effect = dict(tools)
        changed_effect["list_files"] = Tool(
            "list_files",
            tools["list_files"].handler,
            effect="side_effecting",
            tool_revision="1",
        )
        cases.append(tuple(changed_effect.values()))
        changed_revision = dict(tools)
        changed_revision["write_text"] = replace(tools["write_text"], tool_revision="2")
        cases.append(tuple(changed_revision.values()))
        changed_schema = dict(tools)
        changed_schema["read_text"] = Tool(
            "read_text", lambda path, encoding="utf-8": path, effect="read_only"
        )
        cases.append(tuple(changed_schema.values()))
        for changed in cases:
            with self.subTest(changed=changed):
                with self.assertRaises(RegistrationError):
                    validate_registration(
                        self.manifest, replace(self.registration, tools=changed)
                    )

    def test_skills_and_workers_validate_content_and_references(self):
        manifest, registration, skill, worker = self.contribution_fixture()
        validate_registration(manifest, registration)
        cases = [
            replace(registration, skills=(replace(skill, tool_names=("missing",)),)),
            replace(registration, workers=(replace(worker, skill_ids=("missing",)),)),
            replace(registration, workers=(replace(worker, tool_names=("missing",)),)),
            replace(registration, skills=(replace(skill, instructions="changed"),)),
            replace(registration, workers=(replace(worker, system_prompt="changed"),)),
            replace(registration, skills=(replace(skill, title="bad\x00title"),)),
            replace(registration, workers=(replace(worker, model_slot="\ud800"),)),
        ]
        for changed in cases:
            with self.subTest(changed=changed):
                with self.assertRaises(RegistrationError):
                    validate_registration(manifest, changed)
        with self.assertRaises(RegistrationError):
            worker_content_sha256(replace(worker, system_prompt="\ud800"))


class DiscoveryAndLoaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manifest = workspace_manifest()
        self.factory = lambda: workspace_registration(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_discovery_and_disabled_loading_never_import(self):
        entry_point = FakeEntryPoint(self.factory)
        discovered = discover_plugins(entry_points=(entry_point,))
        self.assertEqual(entry_point.load_count, 0)
        self.assertEqual(discovered[0].effective_access, FULL_HOST_PROCESS_PRIVILEGES)
        self.assertEqual(
            load_enabled_plugins({}, grants={}, entry_points=(entry_point,)), ()
        )
        self.assertEqual(entry_point.load_count, 0)

    def test_duplicate_ids_fail_before_import(self):
        first = FakeEntryPoint(self.factory)
        second = FakeEntryPoint(self.factory)
        with self.assertRaises(PluginLoadError):
            load_enabled_plugins(
                {self.manifest.plugin_id: self.manifest},
                grants={self.manifest.plugin_id: workspace_grant()},
                entry_points=(first, second),
            )
        self.assertEqual((first.load_count, second.load_count), (0, 0))

    def test_explicit_matching_plugin_loads_once_with_full_host_disclosure(self):
        entry_point = FakeEntryPoint(self.factory)
        loaded = load_enabled_plugins(
            {self.manifest.plugin_id: self.manifest},
            grants={self.manifest.plugin_id: workspace_grant()},
            entry_points=(entry_point,),
        )
        self.assertEqual(entry_point.load_count, 1)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(
            loaded[0].permissions.effective_access,
            FULL_HOST_PROCESS_PRIVILEGES,
        )
        self.assertFalse(loaded[0].permissions.enforced)
        self.assertTrue(loaded[0].permissions.effective.full_host)

    def test_identity_or_grant_mismatch_fails_before_import(self):
        entries = [
            FakeEntryPoint(self.factory, distribution="other"),
            FakeEntryPoint(self.factory, version="0.2.0"),
            FakeEntryPoint(self.factory, value="sasori_plugins.workspace:other"),
        ]
        for entry_point in entries:
            with self.subTest(value=entry_point.value, dist=entry_point.dist.name):
                with self.assertRaises(PluginLoadError):
                    load_enabled_plugins(
                        {self.manifest.plugin_id: self.manifest},
                        grants={self.manifest.plugin_id: workspace_grant()},
                        entry_points=(entry_point,),
                    )
                self.assertEqual(entry_point.load_count, 0)

        entry_point = FakeEntryPoint(self.factory)
        with self.assertRaises(PluginPermissionError):
            load_enabled_plugins(
                {self.manifest.plugin_id: self.manifest},
                grants={self.manifest.plugin_id: PermissionSet()},
                entry_points=(entry_point,),
            )
        self.assertEqual(entry_point.load_count, 0)
        with self.assertRaises(PluginPermissionError):
            load_enabled_plugins(
                {self.manifest.plugin_id: self.manifest},
                grants={},
                entry_points=(entry_point,),
            )
        self.assertEqual(entry_point.load_count, 0)

    def test_registration_mismatch_is_a_load_error(self):
        entry_point = FakeEntryPoint(
            lambda: replace(workspace_registration(self.temp.name), version="9")
        )
        with self.assertRaises(PluginLoadError):
            load_enabled_plugins(
                {self.manifest.plugin_id: self.manifest},
                grants={self.manifest.plugin_id: workspace_grant()},
                entry_points=(entry_point,),
            )
        self.assertEqual(entry_point.load_count, 1)

    def test_isolated_manifest_is_never_loaded(self):
        entry_point = FakeEntryPoint(self.factory)
        manifest = replace(
            self.manifest,
            execution=replace(self.manifest.execution, mode="container"),
        )
        with self.assertRaises(PluginLoadError):
            load_enabled_plugins(
                {manifest.plugin_id: manifest},
                grants={manifest.plugin_id: workspace_grant()},
                entry_points=(entry_point,),
            )
        self.assertEqual(entry_point.load_count, 0)


class PermissionAndUpgradeTests(unittest.TestCase):
    def test_trusted_permissions_are_disclosure_not_enforcement(self):
        manifest = workspace_manifest()
        with self.assertRaises(PluginPermissionError):
            disclose_permissions(manifest, PermissionSet())
        disclosure = disclose_permissions(manifest, manifest.permissions)
        self.assertEqual(disclosure.requested, manifest.permissions)
        self.assertEqual(disclosure.granted, manifest.permissions)
        self.assertTrue(disclosure.effective.full_host)
        self.assertEqual(
            disclosure.effective_access, FULL_HOST_PROCESS_PRIVILEGES
        )
        self.assertFalse(disclosure.enforced)
        with self.assertRaises((TypeError, ValueError)):
            PermissionSet(filesystem_read="workspace:**/*")
        with self.assertRaises((TypeError, ValueError)):
            PermissionSet(full_host=1)

    def test_isolated_modes_are_parseable_but_have_no_runtime_disclosure(self):
        for mode in ("container", "supervised_process"):
            data = manifest_data()
            data["execution"]["mode"] = mode
            manifest = parse_manifest(manifest_json(data))
            with self.subTest(mode=mode):
                with self.assertRaises(PluginPermissionError):
                    disclose_permissions(manifest, manifest.permissions)

    def test_upgrade_diff_reports_minimum_security_changes(self):
        before_data = manifest_data()
        before_data["execution"]["mode"] = "container"
        before_data["dependencies"] = [{
            "name": "example",
            "version": "1",
            "filename": "example-1.whl",
            "sha256": "0" * 64,
        }]
        before_data["skills"] = [
            {
                "skill_id": "com.sasori.workspace/changed",
                "version": "1",
                "content_sha256": "0" * 64,
            },
            {
                "skill_id": "com.sasori.workspace/removed",
                "version": "1",
                "content_sha256": "0" * 64,
            },
        ]
        before_data["workers"] = [
            {
                "worker_id": "com.sasori.workspace/changed",
                "version": "1",
                "content_sha256": "0" * 64,
            },
            {
                "worker_id": "com.sasori.workspace/removed",
                "version": "1",
                "content_sha256": "0" * 64,
            },
        ]
        after_data = copy.deepcopy(before_data)
        after_data["execution"]["mode"] = "trusted_process"
        after_data["execution"]["entry_point_value"] = (
            "sasori_plugins.workspace:register_v2"
        )
        after_data["permissions"]["filesystem_read"].append("artifacts:reports/*")
        after_data["permissions"]["filesystem_write"].append("temp:generated/*")
        after_data["permissions"]["network_egress"].append("api.example.com:443")
        after_data["permissions"]["secrets"].append("EXAMPLE_API_KEY")
        after_data["tools"][0]["effect"] = "side_effecting"
        after_data["tools"][0]["tool_revision"] = "1"
        after_data["tools"][1]["schema_sha256"] = "1" * 64
        after_data["tools"][3]["tool_revision"] = "2"
        after_data["dependencies"][0] = {
            "name": "example",
            "version": "2",
            "filename": "example-2.whl",
            "sha256": "2" * 64,
        }
        after_data["skills"] = [
            {
                "skill_id": "com.sasori.workspace/changed",
                "version": "2",
                "content_sha256": "1" * 64,
            },
            {
                "skill_id": "com.sasori.workspace/added",
                "version": "1",
                "content_sha256": "1" * 64,
            },
        ]
        after_data["workers"] = [
            {
                "worker_id": "com.sasori.workspace/changed",
                "version": "2",
                "content_sha256": "1" * 64,
            },
            {
                "worker_id": "com.sasori.workspace/added",
                "version": "1",
                "content_sha256": "1" * 64,
            },
        ]

        changes = manifest_upgrade_diff(
            parse_manifest(manifest_json(before_data)),
            parse_manifest(manifest_json(after_data)),
        )
        paths = {change.path: change for change in changes}
        expected = {
            "execution.mode",
            "execution.entry_point_value",
            "permissions.filesystem_read",
            "permissions.filesystem_write",
            "permissions.network_egress",
            "permissions.secrets",
            "tools.list_files.effect",
            "tools.list_files.tool_revision",
            "tools.read_text.schema_sha256",
            "tools.write_text.tool_revision",
            "skills.com.sasori.workspace/changed",
            "skills.com.sasori.workspace/added",
            "workers.com.sasori.workspace/changed",
            "workers.com.sasori.workspace/added",
            "dependencies.example",
        }
        self.assertTrue(expected.issubset(paths))
        self.assertTrue(all(paths[path].requires_approval for path in expected))
        for path in (
            "skills.com.sasori.workspace/removed",
            "workers.com.sasori.workspace/removed",
        ):
            self.assertIn(path, paths)
            self.assertFalse(paths[path].requires_approval)

    def test_upgrade_diff_requires_approval_for_effect_downgrades(self):
        for new_effect in ("read_only", "idempotent"):
            before_data = manifest_data()
            after_data = copy.deepcopy(before_data)
            write_tool = next(
                tool for tool in after_data["tools"] if tool["name"] == "write_text"
            )
            write_tool["effect"] = new_effect
            write_tool["tool_revision"] = "2"

            changes = manifest_upgrade_diff(
                parse_manifest(manifest_json(before_data)),
                parse_manifest(manifest_json(after_data)),
            )
            paths = {change.path: change for change in changes}
            with self.subTest(new_effect=new_effect):
                self.assertTrue(paths["tools.write_text.effect"].requires_approval)
                self.assertTrue(
                    paths["tools.write_text.tool_revision"].requires_approval
                )

    def test_upgrade_diff_rejects_effect_change_without_revision_change(self):
        for new_effect in ("read_only", "idempotent"):
            before_data = manifest_data()
            after_data = copy.deepcopy(before_data)
            write_tool = next(
                tool for tool in after_data["tools"] if tool["name"] == "write_text"
            )
            write_tool["effect"] = new_effect

            with self.subTest(new_effect=new_effect):
                with self.assertRaisesRegex(
                    ValueError,
                    "^tool effect changes require a new tool_revision: write_text$",
                ):
                    manifest_upgrade_diff(
                        parse_manifest(manifest_json(before_data)),
                        parse_manifest(manifest_json(after_data)),
                    )


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def tools(self, limits=None):
        registration = workspace_registration(self.root, limits=limits)
        return {tool.name: tool.handler for tool in registration.tools}

    def make_symlink(self, target, link, *, directory=False):
        try:
            os.symlink(target, link, target_is_directory=directory)
        except (NotImplementedError, OSError) as error:
            self.skipTest(f"symbolic links are unavailable: {error}")

    def test_list_read_and_search_happy_path(self):
        (self.root / "nested").mkdir()
        (self.root / "a.txt").write_bytes(b"alpha\nneedle one\n")
        (self.root / "nested" / "b.txt").write_bytes(b"needle two\n")
        tools = self.tools()
        self.assertEqual(tools["list_files"](), ["a.txt", "nested/b.txt"])
        self.assertEqual(tools["read_text"]("a.txt"), "alpha\nneedle one\n")
        self.assertEqual(
            tools["search_text"]("needle"),
            ["a.txt:2:needle one", "nested/b.txt:1:needle two"],
        )

    def test_depth_count_size_and_output_limits(self):
        (self.root / "one").mkdir()
        (self.root / "one" / "two").mkdir()
        (self.root / "one" / "two" / "deep.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(WorkspaceLimitError):
            self.tools(WorkspaceLimits(max_depth=1))["list_files"]()

        (self.root / "first.txt").write_text("x", encoding="utf-8")
        (self.root / "second.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(WorkspaceLimitError):
            self.tools(WorkspaceLimits(max_files=1))["list_files"]()

        (self.root / "large.txt").write_text("12345", encoding="utf-8")
        with self.assertRaises(WorkspaceLimitError):
            self.tools(WorkspaceLimits(max_file_bytes=4))["read_text"]("large.txt")
        with self.assertRaises(WorkspaceLimitError):
            self.tools(WorkspaceLimits(max_output_chars=4))["read_text"]("large.txt")
        with self.assertRaises(WorkspaceLimitError):
            self.tools(WorkspaceLimits(max_output_chars=4))["list_files"]("first.txt")

    def test_absolute_parent_device_ads_and_unsafe_names_are_rejected(self):
        tools = self.tools()
        cases = [
            str(self.root / "file.txt"),
            "/outside.txt",
            "../outside.txt",
            "folder/../outside.txt",
            "C:\\Windows\\win.ini",
            "\\\\server\\share\\file.txt",
            "CON",
            "CONOUT$.txt",
            "file.txt:stream",
            "trailing. ",
            "bad\nname.txt",
            "\ud800",
        ]
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(WorkspacePathError):
                    tools["read_text"](value)

    def test_binary_and_invalid_utf8_reads_fail_while_directory_search_skips_them(self):
        (self.root / "binary.bin").write_bytes(b"text\x00binary")
        (self.root / "invalid.txt").write_bytes(b"\xff")
        (self.root / "valid.txt").write_text("find me", encoding="utf-8")
        tools = self.tools()
        for name in ("binary.bin", "invalid.txt"):
            with self.subTest(name=name):
                with self.assertRaises(WorkspaceFileError):
                    tools["read_text"](name)
                with self.assertRaises(WorkspaceFileError):
                    tools["search_text"]("find", name)
        self.assertEqual(tools["search_text"]("find"), ["valid.txt:1:find me"])

    def test_outside_and_directory_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as outside_temp:
            outside = Path(outside_temp)
            (outside / "secret.txt").write_text("secret", encoding="utf-8")
            self.make_symlink(outside / "secret.txt", self.root / "outside.txt")
            with self.assertRaises(WorkspacePathError):
                self.tools()["read_text"]("outside.txt")
            with self.assertRaises(WorkspacePathError):
                self.tools()["write_text"]("outside.txt", "changed")
            self.assertEqual(
                (outside / "secret.txt").read_text(encoding="utf-8"), "secret"
            )

        (self.root / "real").mkdir()
        (self.root / "real" / "inside.txt").write_text("inside", encoding="utf-8")
        self.make_symlink(self.root / "real", self.root / "linked", directory=True)
        for path in ("linked", "linked/inside.txt"):
            with self.subTest(path=path):
                with self.assertRaises(WorkspacePathError):
                    self.tools()["list_files"](path)

    def test_in_root_file_symlink_can_be_read_but_never_replaced(self):
        target = self.root / "target.txt"
        target.write_text("inside", encoding="utf-8")
        self.make_symlink(target, self.root / "alias.txt")
        tools = self.tools()
        self.assertEqual(tools["read_text"]("alias.txt"), "inside")
        with self.assertRaises(WorkspacePathError):
            tools["write_text"]("alias.txt", "changed")
        self.assertEqual(target.read_text(encoding="utf-8"), "inside")

    def test_write_creates_and_replaces_one_file_without_temp_artifacts(self):
        tools = self.tools()
        result = tools["write_text"]("note.txt", "first")
        self.assertEqual(result, {"path": "note.txt", "bytes": 5})
        self.assertEqual((self.root / "note.txt").read_text(encoding="utf-8"), "first")
        tools["write_text"]("note.txt", "second")
        self.assertEqual((self.root / "note.txt").read_text(encoding="utf-8"), "second")
        self.assertEqual(list(self.root.glob(".note.txt.*.tmp")), [])
        with self.assertRaises(WorkspaceFileError):
            tools["write_text"]("binary.txt", "bad\x00content")

    def test_write_through_directory_link_is_rejected(self):
        (self.root / "real").mkdir()
        self.make_symlink(self.root / "real", self.root / "linked", directory=True)
        with self.assertRaises(WorkspacePathError):
            self.tools()["write_text"]("linked/new.txt", "unsafe")
        self.assertFalse((self.root / "real" / "new.txt").exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_windows_junction_escape_and_write_are_rejected(self):
        with tempfile.TemporaryDirectory() as outside_temp:
            outside = Path(outside_temp)
            (outside / "secret.txt").write_text("secret", encoding="utf-8")
            junction = self.root / "junction"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                check=False,
                capture_output=True,
                text=True,
            )
            if created.returncode != 0:
                self.skipTest("directory junction creation is unavailable")
            try:
                tools = self.tools()
                with self.assertRaises(WorkspacePathError):
                    tools["list_files"]("junction")
                with self.assertRaises(WorkspacePathError):
                    tools["write_text"]("junction/new.txt", "unsafe")
                self.assertFalse((outside / "new.txt").exists())
            finally:
                os.rmdir(junction)


class WorkspaceHarnessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / "runs.sqlite3"
        self.write_tool = {
            tool.name: tool for tool in workspace_registration(self.root).tools
        }["write_text"]

    def _harness(self, *args, **kwargs):
        return self.enterContext(Harness(*args, **kwargs))

    def _store(self):
        return self.enterContext(SQLiteStore(self.db_path))

    async def test_denial_never_writes(self):
        target = self.root / "denied.txt"

        def final(messages):
            self.assertEqual(messages[-1].error_code, "approval_denied")
            return ModelReply(content="denied")

        harness = self._harness(
            ScriptedModel(
                ModelReply(
                    tool_calls=(
                        ToolCall(
                            "write-denied",
                            "write_text",
                            {"path": "denied.txt", "content": "no"},
                        ),
                    )
                ),
                final,
            ),
            (self.write_tool,),
        )
        with self.assertRaises(RunPaused) as paused:
            await harness.run((Message("user", "write"),), run_id="denied")
        self.assertFalse(target.exists())
        harness.resolve_approval("denied", paused.exception.request.fingerprint, False)
        result = await harness.resume("denied")
        self.assertEqual(result.final_message.content, "denied")
        self.assertFalse(target.exists())

    async def test_approval_writes_once_and_completed_resume_does_not_repeat(self):
        target = self.root / "approved.txt"

        def final(messages):
            self.assertIn('"path":"approved.txt"', messages[-1].content)
            return ModelReply(content="done")

        harness = self._harness(
            ScriptedModel(
                ModelReply(
                    tool_calls=(
                        ToolCall(
                            "write-approved",
                            "write_text",
                            {"path": "approved.txt", "content": "yes"},
                        ),
                    )
                ),
                final,
            ),
            (self.write_tool,),
        )
        with patch(
            "sasori_plugins.workspace.os.replace", wraps=os.replace
        ) as replace_file:
            with self.assertRaises(RunPaused) as paused:
                await harness.run((Message("user", "write"),), run_id="approved")
            self.assertFalse(target.exists())
            self.assertEqual(replace_file.call_count, 0)
            harness.resolve_approval(
                "approved", paused.exception.request.fingerprint, True
            )
            result = await harness.resume("approved")
            self.assertEqual(result.final_message.content, "done")
            self.assertEqual(target.read_text(encoding="utf-8"), "yes")
            self.assertEqual(replace_file.call_count, 1)
            repeated = await harness.resume("approved")
            self.assertEqual(repeated.final_message.content, "done")
            self.assertEqual(replace_file.call_count, 1)

    async def test_old_approval_cannot_execute_after_tool_revision_changes(self):
        store = self._store()
        old = self._harness(
            ScriptedModel(
                ModelReply(
                    tool_calls=(
                        ToolCall(
                            "write-old",
                            "write_text",
                            {"path": "old.txt", "content": "unsafe"},
                        ),
                    )
                )
            ),
            (self.write_tool,),
            store=store,
        )
        with self.assertRaises(RunPaused) as approval:
            await old.run((Message("user", "write"),), run_id="revision")
        old.resolve_approval("revision", approval.exception.request.fingerprint, True)
        store.close()

        reopened = self._store()
        changed = replace(self.write_tool, tool_revision="2")
        with self.assertRaises(RunPaused) as paused:
            await self._harness(
                ScriptedModel(), (changed,), store=reopened
            ).resume("revision")
        self.assertEqual(paused.exception.reason, "tool_contract_changed")
        self.assertFalse((self.root / "old.txt").exists())
        reopened.close()


if __name__ == "__main__":
    unittest.main()
