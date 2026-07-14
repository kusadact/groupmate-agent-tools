from __future__ import annotations

import ast
import asyncio
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(
    os.environ.get(
        "GROUPMATE_AGENT_TOOLS_ROOT",
        str(Path(__file__).resolve().parents[1]),
    )
)
TOOL_MANIFEST = {
    "annual_report": {
        "skill": "annual_report",
        "tools": ("generate_and_send_annual_report",),
    },
    "chat_stats_sql": {
        "skill": "chat_statistics",
        "tools": ("query_chat_stats_sql",),
    },
    "danbooru_setu": {
        "skill": "danbooru_setu",
        "tools": ("send_danbooru_setu",),
    },
    "find_femboy": {
        "skill": "find_femboy",
        "tools": ("find_femboy_in_recent_chat",),
    },
    "gpt_image_agent": {
        "skill": "image_generation",
        "tools": ("generate_and_send_image",),
    },
    "read_forward_message": {
        "skill": "read_forward_message",
        "tools": ("read_forward_message",),
    },
    "recall_message": {
        "skill": "recall_message",
        "tools": ("recall_message",),
    },
    "scheduled_tasks": {
        "skill": "scheduled_tasks",
        "tools": ("schedule_message", "schedule_agent_task"),
    },
    "voice": {
        "skill": "voice_synthesis",
        "tools": ("send_voice",),
    },
    "poke": {
        "skill": "poke",
        "tools": ("poke_user",),
    },
}


def _name(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _string_value(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _tool_names_from_module(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _name(node.func) != "tool" or not node.args:
            continue
        value = _string_value(node.args[0])
        if value:
            names.add(value)
    return names


def _agent_skill_calls(tree: ast.Module) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _name(node.func) == "AgentSkill"
    ]


def _keyword(call: ast.Call, name: str) -> ast.AST | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _literal_strings(node: ast.AST | None) -> tuple[str, ...]:
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(value for item in node.elts if (value := _string_value(item)) is not None)
    return ()


def _load_skill_runtime() -> tuple[Any, Any] | None:
    source_root = Path(
        os.environ.get(
            "GROUPMATE_AGENT_SRC",
            "/Users/kusada/code/nonebot-plugins/nonebot-plugin-ai-groupmate-main/src/nonebot_plugin_groupmate_agent",
        )
    )
    types_path = source_root / "agent/optional_tools/types.py"
    skills_path = source_root / "agent/skills.py"
    if not types_path.is_file() or not skills_path.is_file():
        return None

    package_name = "_groupmate_agent_skill_migration_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(source_root)]
    agent_package = types.ModuleType(f"{package_name}.agent")
    agent_package.__path__ = [str(source_root / "agent")]
    optional_package = types.ModuleType(f"{package_name}.agent.optional_tools")
    optional_package.__path__ = [str(source_root / "agent/optional_tools")]
    sys.modules.update(
        {
            package_name: package,
            f"{package_name}.agent": agent_package,
            f"{package_name}.agent.optional_tools": optional_package,
        }
    )

    log_module = types.ModuleType("nonebot.log")
    log_module.logger = types.SimpleNamespace(warning=lambda *args, **kwargs: None)
    sys.modules.setdefault("nonebot.log", log_module)

    class FakeTool:
        def __init__(self, name: str, function: Any):
            self.name = name
            self._function = function

        async def ainvoke(self, value: Any, **kwargs: Any) -> Any:
            args = value if isinstance(value, dict) else {}
            result = self._function(**args)
            if hasattr(result, "__await__"):
                return await result
            return result

    langchain_tools = types.ModuleType("langchain.tools")

    def tool(name: str):
        def decorator(function: Any) -> FakeTool:
            return FakeTool(name, function)

        return decorator

    langchain_tools.tool = tool
    sys.modules.setdefault("langchain.tools", langchain_tools)

    def load_module(module_name: str, path: Path) -> types.ModuleType:
        spec = importlib.util.spec_from_file_location(module_name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    loaded_types = load_module(f"{package_name}.agent.optional_tools.types", types_path)
    loaded_skills = load_module(f"{package_name}.agent.skills", skills_path)
    return loaded_types, loaded_skills


class AgentSkillMigrationTests(unittest.TestCase):
    def test_every_tool_declares_a_valid_lazy_skill(self):
        for tool_name, expected in TOOL_MANIFEST.items():
            with self.subTest(tool=tool_name):
                source_path = ROOT / "tools" / tool_name / "__init__.py"
                tree = ast.parse(source_path.read_text(encoding="utf-8"))
                skill_calls = _agent_skill_calls(tree)
                self.assertEqual(len(skill_calls), 1)
                skill_call = skill_calls[0]

                self.assertEqual(_string_value(_keyword(skill_call, "name")), expected["skill"])
                self.assertTrue(_string_value(_keyword(skill_call, "description")))
                self.assertIsNotNone(_keyword(skill_call, "prompt"))
                self.assertEqual(_literal_strings(_keyword(skill_call, "tool_names")), expected["tools"])
                self.assertTrue(set(expected["tools"]).issubset(_tool_names_from_module(tree)))

                bundle_calls = [
                    node
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call) and _name(node.func) == "OptionalToolBundle"
                ]
                self.assertTrue(bundle_calls)
                self.assertTrue(any(_keyword(node, "skills") is not None for node in bundle_calls))
                self.assertTrue(all(_keyword(node, "prompt") is None for node in bundle_calls))

    def test_skill_gate_hides_tools_until_skill_is_loaded(self):
        runtime = _load_skill_runtime()
        if runtime is None:
            self.skipTest("main plugin source is unavailable")
        types_module, skills_module = runtime

        class FakeTool:
            def __init__(self, name: str):
                self.name = name

        base_tool = FakeTool("base_tool")
        lazy_tool = FakeTool("lazy_tool")
        skill = types_module.AgentSkill(
            name="lazy_skill",
            description="按需工具",
            prompt="完整规则",
            tool_names=("lazy_tool",),
        )
        setup = skills_module.prepare_agent_skill_tools([base_tool, lazy_tool], [skill])

        self.assertEqual([item.name for item in setup.base_tools], ["base_tool"])
        self.assertEqual([item.name for item in setup.tools_by_skill["lazy_skill"]], ["lazy_tool"])

        loader = skills_module.create_agent_skill_loader_tool([skill], types.SimpleNamespace(session_id="group-1"))
        self.assertIsNotNone(loader)
        self.assertEqual(asyncio.run(loader.ainvoke({"skill_name": "lazy_skill"})), "完整规则")
        active_tools = list(setup.base_tools) + list(setup.tools_by_skill["lazy_skill"])
        self.assertEqual([item.name for item in active_tools], ["base_tool", "lazy_tool"])

    def test_failed_skill_prompt_does_not_activate_skill(self):
        runtime = _load_skill_runtime()
        if runtime is None:
            self.skipTest("main plugin source is unavailable")
        types_module, skills_module = runtime

        async def broken_prompt(ctx):
            raise RuntimeError("prompt unavailable")

        skill = types_module.AgentSkill(
            name="broken_skill",
            description="失败技能",
            prompt=broken_prompt,
            tool_names=("lazy_tool",),
        )
        loader = skills_module.create_agent_skill_loader_tool([skill], types.SimpleNamespace(session_id="group-1"))
        active_skills: list[str] = []
        with self.assertRaisesRegex(RuntimeError, "prompt unavailable"):
            asyncio.run(loader.ainvoke({"skill_name": "broken_skill"}))
        self.assertEqual(active_skills, [])

    def test_legacy_bundle_without_skills_keeps_tools_visible(self):
        runtime = _load_skill_runtime()
        if runtime is None:
            self.skipTest("main plugin source is unavailable")
        _, skills_module = runtime

        class FakeTool:
            def __init__(self, name: str):
                self.name = name

        setup = skills_module.prepare_agent_skill_tools([FakeTool("legacy_tool")], [])
        self.assertEqual([item.name for item in setup.base_tools], ["legacy_tool"])
        self.assertEqual(setup.tools_by_skill, {})


if __name__ == "__main__":
    unittest.main()
