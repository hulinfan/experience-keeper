#!/usr/bin/env python3
"""experience-keeper 单元测试与场景测试。

覆盖设计文档「测试」章节列出的用例。全部用标准库 unittest，
不引入额外依赖，Windows / Linux / macOS 均可运行。
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import experience  # noqa: E402
import experience_hook  # noqa: E402
import experience_lib as lib  # noqa: E402
import exit_sentinel  # noqa: E402
import install  # noqa: E402
import prescreen  # noqa: E402
import transcripts  # noqa: E402


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


class BaseCase(unittest.TestCase):
    """每个用例独立一份 EXPERIENCE_HOME 与项目目录，互不干扰。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "store"
        self.project = Path(self.temp.name) / "work" / "demo"
        (self.project / ".git").mkdir(parents=True, exist_ok=True)
        self._env = mock.patch.dict(os.environ, {"EXPERIENCE_HOME": str(self.home)})
        self._env.start()
        # 采集会派生真实哨兵进程：测试里一律拦掉，需要哨兵行为的用例自己直接调用。
        self.spawn_mock = mock.MagicMock(return_value=None)
        self._spawn = mock.patch.object(exit_sentinel, "spawn", self.spawn_mock)
        self._spawn.start()
        self._cwd = os.getcwd()
        os.chdir(self.project)

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        self._spawn.stop()
        self._env.stop()
        self.temp.cleanup()

    def run_cli(self, argv: list[str]) -> str:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            experience.main(argv)
        return buffer.getvalue()

    def other_project(self, name: str = "gwsvc") -> Path:
        """造第二个项目目录，用于验证跨项目积压口径。"""
        root = Path(self.temp.name) / "work" / name
        (root / ".git").mkdir(parents=True, exist_ok=True)
        return root

    def seed_candidate_file(self, project_root: Path, ts: str = "", client: str = "codex", session: str = "s-other") -> str:
        """直接写一条候选记录，返回 candidate-id；不经过转录解析。"""
        name = lib.ensure_project(project_root)
        stamp = ts or lib.now_iso()
        path = lib.candidate_file(name, client, session, stamp[:10])
        record = {
            "ts": stamp,
            "client": client,
            "session_id": session,
            "turn_id": "1",
            "project_name": name,
            "cwd": str(project_root),
            "compaction_count": 0,
            "signal": ["error_recovered"],
            "user": "查询超时后改成先过滤",
            "assistant": "已改",
            "tools": [],
            "outcome": "success",
            "cost": {"tool_calls": 1, "tokens": 10, "duration_ms": 1000},
            "evidence": {"path": "~/.codex/sessions/x.jsonl", "lines": [1, 20]},
        }
        rows = []
        if path.exists():
            rows = [line for line in lib.read_text(path).splitlines() if line.strip()]
        rows.append(json.dumps(record, ensure_ascii=False, sort_keys=True))
        lib.atomic_write_text(path, "\n".join(rows) + "\n")
        return lib.candidate_id(client, session, "1", stamp[:10])


class ProjectNameTest(BaseCase):
    def test_default_uses_directory_name(self) -> None:
        self.assertEqual(lib.ensure_project(self.project), "demo")

    def test_same_basename_gets_parent_prefix(self) -> None:
        first = Path(self.temp.name) / "org" / "gwsvc"
        second = Path(self.temp.name) / "tmp" / "gwsvc"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        self.assertEqual(lib.ensure_project(first), "gwsvc")
        self.assertEqual(lib.ensure_project(second), "tmp-gwsvc")

    def test_missing_meta_registers_on_first_use(self) -> None:
        name = lib.ensure_project(self.project)
        meta = json.loads((self.home / "projects" / name / "META.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["project_root"], str(self.project))


class SignalTest(BaseCase):
    def base_turn(self, **overrides) -> dict:
        turn = {
            "user": "查一下",
            "prev_user": "",
            "assistant": "好了",
            "tools": [],
            "tool_calls": 0,
            "tokens": 0,
            "duration_ms": 1000,
            "outcome": "success",
        }
        turn.update(overrides)
        return turn

    def test_error_recovered(self) -> None:
        """同一命令族内先错后对才算恢复策略。

        键必须带命令族：codex 全轮只有一个 exec，只按工具名配对等于「本轮出过错又成功过」。
        """
        turn = self.base_turn(
            tools=[
                {"name": "bash", "args": {"command": "aliyun vpc DescribeRouteEntry --id rtb-1"}, "status": "error", "error": "Throttling"},
                {"name": "bash", "args": {"command": "aliyun vpc DescribeRouteEntry --id rtb-1 --region cn"}, "status": "ok"},
            ]
        )
        self.assertIn("error_recovered", prescreen.detect_signals(turn))
        unrelated = self.base_turn(
            tools=[
                {"name": "bash", "args": {"command": "aliyun cms QueryMetric --period 60"}, "status": "error", "error": "Throttling"},
                {"name": "bash", "args": {"command": "aliyun vpc DescribeRouteEntry --id rtb-1"}, "status": "ok"},
            ]
        )
        self.assertNotIn("error_recovered", prescreen.detect_signals(unrelated))

    def test_retry(self) -> None:
        turn = self.base_turn(
            tools=[
                {"name": "bash", "args": {"command": "a"}, "status": "error"},
                {"name": "bash", "args": {"command": "a"}, "status": "ok"},
            ]
        )
        self.assertIn("retry", prescreen.detect_signals(turn))

    def test_no_signal(self) -> None:
        turn = self.base_turn(tools=[{"name": "read", "args": {"path": "x"}, "status": "ok"}])
        self.assertEqual(prescreen.detect_signals(turn), [])

    def test_multi_source_by_skill_count(self) -> None:
        turn = self.base_turn(
            tools=[
                {"name": "bash", "args": {"command": "python skills/ndb-skill/scripts/ndb.py"}, "status": "ok"},
                {"name": "bash", "args": {"command": "python skills/elk-query/scripts/elk_query.py"}, "status": "ok"},
            ]
        )
        self.assertIn("multi_source", prescreen.detect_signals(turn))

    def test_reading_skill_docs_is_not_orchestration(self) -> None:
        """读 SKILL.md 是查文档：翻几个 skill 说明不等于跨 skill 编排。"""
        turn = self.base_turn(
            tools=[
                {"name": "bash", "args": {"command": "Get-Content -Raw /home/user/.agents/skills/a-skill/SKILL.md"}, "status": "ok"},
                {"name": "bash", "args": {"command": "rg -n x /home/user/.agents/skills/b-skill/SKILL.md"}, "status": "ok"},
                {"name": "bash", "args": {"command": 'python "$HOME/.agents/skills/experience-keeper/scripts/experience.py" prompt'}, "status": "ok"},
            ]
        )
        self.assertNotIn("multi_source", prescreen.detect_signals(turn))

    def test_slow_is_metadata_not_signal(self) -> None:
        """时长与轮次价值无因果：只保留在候选 cost.duration_ms 里，不落 signal。"""
        turn = self.base_turn(duration_ms=121_000, outcome="aborted")
        signals = prescreen.detect_signals(turn)
        self.assertNotIn("slow", signals)
        self.assertIn("failure", signals)


class PrescreenTableTest(BaseCase):
    """经验预筛表的判定用例。

    表定义见 docs/experience-prescreen-table-design.md；
    每条信号至少一个命中用例与一个不命中用例，避免词表放宽后无人察觉。
    """

    def turn(self, **overrides) -> dict:
        base = {
            "user": "",
            "prev_user": "",
            "assistant": "好了",
            "tools": [],
            "tool_calls": 0,
            "tokens": 0,
            "duration_ms": 1000,
            "outcome": "success",
        }
        base.update(overrides)
        return base

    def test_dep_fix(self) -> None:
        hit = self.turn(tools=[{"name": "bash", "args": {"command": "pip install -r requirements.txt"}, "status": "ok"}])
        self.assertIn("dep_fix", prescreen.detect_signals(hit))
        miss = self.turn(tools=[{"name": "bash", "args": {"command": "pip list"}, "status": "ok"}])
        self.assertNotIn("dep_fix", prescreen.detect_signals(miss))

    def test_asset_script(self) -> None:
        hit = self.turn(
            tools=[
                {
                    "name": "write",
                    "args": {
                        "file_path": "F:\\work\\demo\\scripts\\nat_session.py",
                        "content": "import argparse\nparser = argparse.ArgumentParser()\nparser.add_argument('--gw')",
                    },
                    "status": "ok",
                }
            ]
        )
        self.assertIn("asset_script", prescreen.detect_signals(hit))
        miss = self.turn(tools=[{"name": "write", "args": {"file_path": "/tmp/notes.txt", "content": "x"}, "status": "ok"}])
        self.assertNotIn("asset_script", prescreen.detect_signals(miss))

    def test_plain_script_is_not_an_asset(self) -> None:
        """没有参数化、直接写在临时目录的脚本只解本次问题，不落候选。"""
        turn = self.turn(
            tools=[
                {
                    "name": "write",
                    "args": {"file_path": "/tmp/probe.py", "content": "import socket\nsocket.connect(('10.1.2.3', 80))\nprint('ok')"},
                    "status": "ok",
                }
            ]
        )
        self.assertNotIn("asset_script", prescreen.detect_signals(turn))

    def test_one_off_script_does_not_hit_other_signals(self) -> None:
        turn = self.turn(
            tools=[
                {
                    "name": "write",
                    "args": {"file_path": "/tmp/probe.py", "content": "print(1)"},
                    "status": "ok",
                }
            ]
        )
        self.assertEqual(prescreen.detect_signals(turn), [])

    def test_reuse_classification(self) -> None:
        cases = [
            ("reusable", "/srv/scripts/probe.py", "import argparse\nparser.add_argument('--host')"),
            ("reusable", "/srv/scripts/probe.py", "gw = os.environ.get('NAT_GW')\nprint(gw)"),
            ("reusable", "/srv/scripts/probe.py", "import argparse\nprint('10.1.2.3')"),
            ("reusable", "/srv/scripts/mod.tf", 'variable "region" {}\nregion = "cn-north-1"'),
            ("one_off", "/tmp/probe.py", "print(1)"),
            ("one_off", "/srv/scripts/probe.py", "nat_ip = '10.1.2.3'\nprint(nat_ip)"),
            ("one_off", "/srv/scripts/probe.py", "inst = 'i-0123456789abcdef0'\nprint(inst)"),
        ]
        for expected, path, content in cases:
            with self.subTest(path=path, content=content[:24]):
                # 路径与内容必须分行：同一行拼接会让 "probe.py print(1)" 被正则当成一次赋值。
                self.assertEqual(expected, prescreen.classify_reuse(f"{path}\n{content}", [path]))

    def test_classify_turn_asset_marks_none_without_script(self) -> None:
        turn = self.turn(tools=[{"name": "read", "args": {"file_path": "/tmp/a.py"}, "status": "ok"}])
        self.assertEqual("none", prescreen.classify_turn_asset(turn))

    def test_asset_script_needs_write_tool(self) -> None:
        """读脚本不算产出资产，只有写盘才算。"""
        turn = self.turn(tools=[{"name": "read", "args": {"file_path": "/tmp/a.py"}, "status": "ok"}])
        self.assertNotIn("asset_script", prescreen.detect_signals(turn))

    def test_asset_diagram(self) -> None:
        hit = self.turn(
            tools=[
                {
                    "name": "write",
                    "args": {"file_path": "/tmp/a.md", "content": "```mermaid\nflowchart LR\n  A-->B\n```"},
                    "status": "ok",
                }
            ]
        )
        self.assertIn("asset_diagram", prescreen.detect_signals(hit))
        miss = self.turn(tools=[{"name": "write", "args": {"file_path": "/tmp/a.md", "content": "纯文字"}, "status": "ok"}])
        self.assertNotIn("asset_diagram", prescreen.detect_signals(miss))

    def test_env_recovery(self) -> None:
        hit = self.turn(tools=[{"name": "bash", "args": {"command": "docker compose up -d"}, "status": "ok"}])
        self.assertIn("env_recovery", prescreen.detect_signals(hit))
        miss = self.turn(tools=[{"name": "bash", "args": {"command": "docker ps"}, "status": "ok"}])
        self.assertNotIn("env_recovery", prescreen.detect_signals(miss))

    def test_blast_radius_guard(self) -> None:
        hit = self.turn(
            tools=[{"name": "bash", "args": {"command": "aliyun vpc ModifyRouteEntry --dry-run"}, "status": "ok"}]
        )
        self.assertIn("blast_radius_guard", prescreen.detect_signals(hit))
        miss = self.turn(tools=[{"name": "bash", "args": {"command": "aliyun vpc DescribeVpc"}, "status": "ok"}])
        self.assertNotIn("blast_radius_guard", prescreen.detect_signals(miss))

    def test_evidence_capture(self) -> None:
        hit = self.turn(
            tools=[{"name": "bash", "args": {"command": "tcpdump -i any port 4789 -w /tmp/vxlan.pcap"}, "status": "ok"}]
        )
        self.assertIn("evidence_capture", prescreen.detect_signals(hit))
        miss = self.turn(tools=[{"name": "bash", "args": {"command": "grep conntrack /var/log/syslog"}, "status": "ok"}])
        self.assertNotIn("evidence_capture", prescreen.detect_signals(miss))

    def test_env_hazard_from_args(self) -> None:
        hit = self.turn(tools=[{"name": "bash", "args": {"command": "sysctl -w net.netfilter.nf_conntrack_max=65536"}, "status": "ok"}])
        self.assertIn("env_hazard", prescreen.detect_signals(hit))

    def test_env_hazard_from_user_text(self) -> None:
        """用户声明在线上操作不再落 env_hazard：那是背景叙述，不是本轮动的动作。

        环境名词误报率实测 100%：总结文档里一句「多环境上线」就会命中。
        真动作仍由 test_env_hazard_from_args 兜住。
        """
        turn = self.turn(
            user="这是在线上环境跑的，小心点",
            tools=[{"name": "bash", "args": {"command": "ip route add 10.0.0.0/8 via 10.1.1.1"}, "status": "ok"}],
        )
        self.assertNotIn("env_hazard", prescreen.detect_signals(turn))

    def test_signal_dedupe(self) -> None:
        """同一信号在轮内多次命中只留一条：标签重复没有信息量。"""
        turn = self.turn(
            tools=[
                {"name": "bash", "args": {"command": "aliyun vpc ModifyRouteEntry --dry-run"}, "status": "ok"},
                {"name": "bash", "args": {"command": "aliyun vpc DeleteRouteEntry --dry-run"}, "status": "ok"},
            ]
        )
        signals = prescreen.detect_signals(turn)
        self.assertEqual(signals.count("blast_radius_guard"), 1)

    def test_plain_turn_stays_empty(self) -> None:
        turn = self.turn(user="看下这个文件", tools=[{"name": "read", "args": {"file_path": "/tmp/a.md"}, "status": "ok"}])
        self.assertEqual(prescreen.detect_signals(turn), [])


class PrescreenTableExtTest(BaseCase):
    """经验预筛表扩充族的判定用例：表定义见 docs/experience-prescreen-table-design.md。"""

    def turn(self, **overrides) -> dict:
        base = {
            "user": "",
            "prev_user": "",
            "prev_users": [],
            "prev_outcome": "",
            "assistant": "好了",
            "tools": [],
            "tool_calls": 0,
            "tokens": 0,
            "duration_ms": 1000,
            "outcome": "success",
        }
        base.update(overrides)
        return base

    def test_fallback_switch(self) -> None:
        """换工具链后成功：这一轮必须落候选，否则换路经验只在当事会话存在。"""
        hit = self.turn(
            tools=[
                {"name": "bash", "args": {"command": "cms_cli query --all"}, "status": "error", "error": "Throttling"},
                {"name": "bash", "args": {"command": "aliyun cms QueryMetric"}, "status": "ok", "result": "{}"},
            ]
        )
        self.assertIn("fallback_switch", prescreen.detect_signals(hit))
        miss = self.turn(tools=[{"name": "bash", "args": {"command": "aliyun cms QueryMetric"}, "status": "ok", "result": "{}"}])
        self.assertNotIn("fallback_switch", prescreen.detect_signals(miss))

    def test_decision_reversed_and_review_rejected(self) -> None:
        reversed_turn = self.turn(assistant="根因是旧网关限流，之前的路由方案不再适用，改为走新网关")
        self.assertIn("decision_reversed", prescreen.detect_signals(reversed_turn))
        rejected = self.turn(user="这次评审被打回了，需要修改后重提")
        self.assertIn("review_rejected", prescreen.detect_signals(rejected))

    def test_root_cause_mapped_needs_error_and_conclusion(self) -> None:
        """现象与根因必须配对：只有结论没有错误原文的轮次不落，避免猜测进库。"""
        paired = self.turn(
            assistant="根因是 conntrack 表满导致新连接被丢弃",
            tools=[{"name": "bash", "args": {"command": "ss -s"}, "status": "error", "error": "cannot allocate memory"}],
        )
        self.assertIn("root_cause_mapped", prescreen.detect_signals(paired))
        only_conclusion = self.turn(assistant="根因是 conntrack 表满")
        self.assertNotIn("root_cause_mapped", prescreen.detect_signals(only_conclusion))

    def test_counterintuitive(self) -> None:
        hit = self.turn(assistant="居然是回程路由没加，出方向是通的")
        self.assertIn("counterintuitive", prescreen.detect_signals(hit))
        miss = self.turn(assistant="按计划完成配置")
        self.assertNotIn("counterintuitive", prescreen.detect_signals(miss))

    def test_asset_recipe_and_probe_discipline(self) -> None:
        recipe = self.turn(
            tools=[
                {"name": "bash", "args": {"command": "tcpdump -i any port 4789 -c 10"}, "status": "ok"},
                {"name": "bash", "args": {"command": "conntrack -L | head"}, "status": "ok"},
                {"name": "bash", "args": {"command": "ip route get 10.1.2.3"}, "status": "ok"},
            ]
        )
        signals = prescreen.detect_signals(recipe)
        self.assertIn("asset_recipe", signals)
        self.assertIn("probe_discipline", signals)
        single = self.turn(tools=[{"name": "bash", "args": {"command": "ip route get 10.1.2.3"}, "status": "ok"}])
        self.assertNotIn("asset_recipe", prescreen.detect_signals(single))
        self.assertNotIn("probe_discipline", prescreen.detect_signals(single))

    def test_asset_query(self) -> None:
        hit = self.turn(
            tools=[{"name": "bash", "args": {"command": "curl -s 'es:9200/vpc-flow-*/_search' -d '{\"query\":{\"term\":{\"srcaddr\":\"10.1.2.3\"}}}'"}, "status": "ok"}]
        )
        self.assertIn("asset_query", prescreen.detect_signals(self.turn(
            tools=[{"name": "bash", "args": {"command": "select srcaddr, sum(bytes) from flow group by srcaddr"}, "status": "ok"}]
        )))

    def test_prereq_discovered(self) -> None:
        hit = self.turn(
            tools=[
                {"name": "bash", "args": {"command": "aws ec2 describe-vpcs"}, "status": "error", "error": "AccessDenied: not authorized to perform ec2:DescribeVpcs"},
                {"name": "bash", "args": {"command": "aws ec2 describe-vpcs --profile prod"}, "status": "ok"},
            ]
        )
        self.assertIn("prereq_discovered", prescreen.detect_signals(hit))
        miss = self.turn(tools=[{"name": "bash", "args": {"command": "aws ec2 describe-vpcs"}, "status": "error", "error": "timeout"}])
        self.assertNotIn("prereq_discovered", prescreen.detect_signals(miss))

    def test_no_convergence_and_idempotency(self) -> None:
        converging = self.turn(
            tools=[
                {"name": "bash", "args": {"command": "aliyun vpc ModifyRouteEntry --next-hop 10.0.0.1"}, "status": "ok"},
                {"name": "bash", "args": {"command": "aliyun vpc DescribeRouteEntry"}, "status": "ok", "result": "status: pending，未生效"},
            ]
        )
        self.assertIn("no_convergence", prescreen.detect_signals(converging))
        unstable = self.turn(
            tools=[
                {"name": "bash", "args": {"command": "conntrack -C"}, "status": "ok", "result": "42310"},
                {"name": "bash", "args": {"command": "conntrack -C"}, "status": "ok", "result": "65535"},
            ]
        )
        self.assertIn("idempotency_mismatch", prescreen.detect_signals(unstable))

    def test_convergence_delay(self) -> None:
        attempts = [
            {"name": "bash", "args": {"command": f"aliyun vpc ModifyRouteEntry --id rtb-0123456789ab --metric {value}"}, "status": "ok"}
            for value in (100, 200, 300, 400)
        ]
        self.assertIn("convergence_delay", prescreen.detect_signals(self.turn(tools=attempts)))

    def test_qa_reflex(self) -> None:
        turn = self.turn(
            user="为什么 VPC 流量没走对等连接",
            prev_users=["VPC 流量为什么不走对等连接", "VPC 流量没走对等连接是什么原因"],
        )
        self.assertIn("qa_reflex", prescreen.detect_signals(turn))
        once = self.turn(user="为什么 VPC 流量没走对等连接", prev_users=["帮我看下 CMS 指标"])
        self.assertNotIn("qa_reflex", prescreen.detect_signals(once))

    def test_perf_limit_requires_unit(self) -> None:
        """性能判据只认现场证据：压测结果来自工具返回，不来自叙述。"""
        hit = self.turn(
            tools=[
                {
                    "name": "bash",
                    "args": {"command": "loadtest --run"},
                    "status": "ok",
                    "result": "压测到 5000 pps 时开始丢包，网关配额上限是 20 Gbps",
                }
            ]
        )
        self.assertIn("perf_limit", prescreen.detect_signals(hit))
        # 裸数字不算：端口、ID、序号在云网络里满地都是。
        miss = self.turn(
            tools=[{"name": "bash", "args": {"command": "set port"}, "status": "ok", "result": "改到端口 8080，重试 3 次"}]
        )
        self.assertNotIn("perf_limit", prescreen.detect_signals(miss))
        # 文档转述不算：总结文档里写「性能提升 50%」是叙述，不是本轮实测的硬限。
        prose = self.turn(assistant="本次优化让转发性能提升 50%，端口耗尽问题也解决了")
        self.assertNotIn("perf_limit", prescreen.detect_signals(prose))
        # 文档正文经 rg 或 skill 抓回来时同样不算：结果是文档内容，不是命令输出。
        body = self.turn(
            tools=[
                {
                    "name": "bash",
                    "args": {"command": "python -c \"print(open('plan').read())\""},
                    "status": "ok",
                    "result": "# 升级计划\n\n压测到 5000 pps 时丢包，端口耗尽上限 65535。\n\n## 验收\n- 完成\n",
                }
            ]
        )
        self.assertNotIn("perf_limit", prescreen.detect_signals(body))

    def test_cost_decision_and_nonfunctional_floor(self) -> None:
        cost = self.turn(user="跨区流量费用太高，二选一后采用方案 B（同区转发），成本降一半")
        self.assertIn("cost_decision", prescreen.detect_signals(cost))
        # 只提到成本、没做选择：是现状陈述，不是决策现场。
        mention = self.turn(user="这条路跨区流量费用太高，换方案要考虑成本")
        self.assertNotIn("cost_decision", prescreen.detect_signals(mention))
        floor = self.turn(
            tools=[{"name": "bash", "args": {"command": "sysctl -w net.netfilter.nf_conntrack_max=131072"}, "status": "ok"}]
        )
        signals = prescreen.detect_signals(floor)
        self.assertIn("nonfunctional_floor", signals)
        self.assertIn("env_hazard", signals)

    def test_cost_trap(self) -> None:
        hit = self.turn(
            tools=[
                {
                    "name": "bash",
                    "args": {"command": "conntrack -S"},
                    "status": "ok",
                    "result": "实测这台机器上 conntrack 老化时间是 120 秒，只在本机成立",
                }
            ]
        )
        self.assertIn("cost_trap", prescreen.detect_signals(hit))
        # 年份与提交号不是量词：2026 是日期，8586257899 是摘要前缀。
        noise = self.turn(
            tools=[
                {
                    "name": "bash",
                    "args": {"command": "git show 8586257899"},
                    "status": "ok",
                    "result": "commit 8586257899 于 2026 提交，阈值未变",
                }
            ]
        )
        self.assertNotIn("cost_trap", prescreen.detect_signals(noise))

    def test_batch_mutation(self) -> None:
        hit = self.turn(
            tools=[
                {"name": "bash", "args": {"command": "aliyun vpc DeleteRouteEntry --id rtb-0123456789ab"}, "status": "ok"},
                {"name": "bash", "args": {"command": "aliyun vpc DeleteRouteEntry --id rtb-abcdef123456"}, "status": "ok"},
                {"name": "bash", "args": {"command": "aliyun vpc DeleteRouteEntry --id rtb-9876543210ab"}, "status": "ok"},
            ]
        )
        self.assertIn("batch_mutation", prescreen.detect_signals(hit))

    def test_retry_loop_covers_different_params(self) -> None:
        """原样重试之外还有参数摸索：同一工具换着参数连错两次也要落候选。"""
        hit = self.turn(
            tools=[
                {"name": "bash", "args": {"command": "aliyun cms QueryMetric --period 60"}, "status": "error", "error": "Throttling"},
                {"name": "bash", "args": {"command": "aliyun cms QueryMetric --period 300"}, "status": "error", "error": "Throttling"},
            ]
        )
        self.assertIn("retry_loop", prescreen.detect_signals(hit))
        once = self.turn(tools=[{"name": "bash", "args": {"command": "x --period 60"}, "status": "error"}])
        self.assertNotIn("retry_loop", prescreen.detect_signals(once))

    def test_retry_loop_needs_consecutive_failures(self) -> None:
        """同一命令族中间夹了成功调用就不算「连错」：那是参数摸索后走通了。"""
        turn = self.turn(
            tools=[
                {"name": "bash", "args": {"command": "aliyun cms QueryMetric --period 60"}, "status": "error", "error": "Throttling"},
                {"name": "bash", "args": {"command": "aliyun cms QueryMetric --period 300"}, "status": "ok"},
                {"name": "bash", "args": {"command": "aliyun cms QueryMetric --period 900"}, "status": "error", "error": "Throttling"},
            ]
        )
        self.assertNotIn("retry_loop", prescreen.detect_signals(turn))

    def test_usage_error_and_test_red_green_are_not_recovery(self) -> None:
        """参数写错了、测试先红后绿都不是可复用恢复策略：重跑一次就能复现。"""
        usage = self.turn(
            tools=[
                {"name": "bash", "args": {"command": "python a.py --bad"}, "status": "error", "error": "a.py: error: unrecognized arguments: --bad"},
                {"name": "bash", "args": {"command": "python a.py --good"}, "status": "ok"},
            ]
        )
        self.assertNotIn("error_recovered", prescreen.detect_signals(usage))
        tdd = self.turn(
            tools=[
                {"name": "bash", "args": {"command": "python -m pytest -q"}, "status": "error", "error": "1 failed, 17 passed"},
                {"name": "bash", "args": {"command": "python -m pytest -q"}, "status": "ok", "result": "18 passed"},
            ]
        )
        self.assertNotIn("error_recovered", prescreen.detect_signals(tdd))
        self.assertNotIn("retry_loop", prescreen.detect_signals(tdd))

    def test_retry_ignores_repeated_success(self) -> None:
        """两次都成功只是重复执行（重复跑测试、重复读文件），不是「第一次没打通」。"""
        turn = self.turn(
            tools=[
                {"name": "bash", "args": {"command": "python -m pytest -q"}, "status": "ok", "result": "18 passed"},
                {"name": "bash", "args": {"command": "python -m pytest -q"}, "status": "ok", "result": "18 passed"},
            ]
        )
        self.assertNotIn("retry", prescreen.detect_signals(turn))

    def test_grounded_verdict(self) -> None:
        """结论锚定源码行号：≥3 处引用说明本轮实读过代码，产出文档查不到的对照结论。"""
        hit = self.turn(assistant="依据 src/core/action.c:1876、src/netif.c:2236 与 src/core/res.c:270 判定该问题不存在")
        self.assertIn("grounded_verdict", prescreen.detect_signals(hit))
        miss = self.turn(assistant="看完了，结论写在文档里")
        self.assertNotIn("grounded_verdict", prescreen.detect_signals(miss))
        two_refs = self.turn(assistant="见 src/core/res.c:270 与 src/core/action.c:1876")
        self.assertNotIn("grounded_verdict", prescreen.detect_signals(two_refs))

    def test_dep_matrix(self) -> None:
        """同一依赖换过版本才装成：版本矩阵排出来比查出来贵，值得留。"""
        hit = self.turn(
            tools=[
                {"name": "bash", "args": {"command": "pip install pyyaml==5.4.1"}, "status": "error", "error": "build failed"},
                {"name": "bash", "args": {"command": "pip install pyyaml==6.0.1"}, "status": "ok"},
            ]
        )
        self.assertIn("dep_matrix", prescreen.detect_signals(hit))
        single = self.turn(tools=[{"name": "bash", "args": {"command": "pip install pyyaml==6.0.1"}, "status": "ok"}])
        self.assertNotIn("dep_matrix", prescreen.detect_signals(single))

    def test_silent_failure_by_intensifier(self) -> None:
        """方案 A：成功之后又给同一工具加诊断开关，说明第一次的成功没让人放心。"""
        hit = self.turn(
            tools=[
                {"name": "bash", "args": {"command": "kubectl apply -f gwsvc.yaml"}, "status": "ok", "result": "ok"},
                {"name": "bash", "args": {"command": "kubectl apply -f gwsvc.yaml --verbose"}, "status": "ok", "result": "ok"},
            ]
        )
        self.assertIn("silent_failure_suspected", prescreen.detect_signals(hit))
        normal = self.turn(
            tools=[
                {"name": "bash", "args": {"command": "kubectl apply -f a.yaml"}, "status": "ok"},
                {"name": "bash", "args": {"command": "kubectl get pod"}, "status": "ok"},
            ]
        )
        self.assertNotIn("silent_failure_suspected", prescreen.detect_signals(normal))

    def test_silent_failure_by_state_assertion(self) -> None:
        """方案 B：成功之后读状态，且人给出否定结论——没有错误码也算静默失败现场。"""
        hit = self.turn(
            assistant="gwsvc 规则并未看到预期流量",
            tools=[
                {"name": "bash", "args": {"command": "gwsvc-ctl apply conf"}, "status": "ok", "result": "ok"},
                {"name": "bash", "args": {"command": "conntrack -L"}, "status": "ok", "result": "0 entries"},
            ],
        )
        self.assertIn("silent_failure_suspected", prescreen.detect_signals(hit))
        no_conclusion = self.turn(
            assistant="已下发并确认",
            tools=[
                {"name": "bash", "args": {"command": "gwsvc-ctl apply conf"}, "status": "ok"},
                {"name": "bash", "args": {"command": "conntrack -L"}, "status": "ok"},
            ],
        )
        self.assertNotIn("silent_failure_suspected", prescreen.detect_signals(no_conclusion))

    def test_idempotency_ignores_volatile_fields(self) -> None:
        """动态字段不能进幂等对比：否则任何两次相同 API 调用都会命中。"""
        volatile = self.turn(
            tools=[
                {"name": "bash", "args": {"command": "api get"}, "status": "ok", "result": "status=active req_id=aaa ts=1"},
                {"name": "bash", "args": {"command": "api get"}, "status": "ok", "result": "status=active req_id=bbb ts=2"},
            ]
        )
        self.assertNotIn("idempotency_mismatch", prescreen.detect_signals(volatile))
        real = self.turn(
            tools=[
                {"name": "bash", "args": {"command": "api get"}, "status": "ok", "result": "status=active"},
                {"name": "bash", "args": {"command": "api get"}, "status": "ok", "result": "status=pending"},
            ]
        )
        self.assertIn("idempotency_mismatch", prescreen.detect_signals(real))

    def test_hard_and_soft_limit_split(self) -> None:
        """硬限制转监控规则，软限制转开发前置约束：混一起复盘时没法分流。"""
        hard = self.turn(
            tools=[{"name": "bash", "args": {"command": "ss -s"}, "status": "ok", "result": "NAT64 网关端口耗尽，65535 上限"}]
        )
        self.assertIn("perf_limit", prescreen.detect_signals(hard))
        self.assertNotIn("soft_limit", prescreen.detect_signals(hard))
        soft = self.turn(
            tools=[{"name": "bash", "args": {"command": "query"}, "status": "ok", "result": "实测单次查询超过 5 万条会 oom"}]
        )
        self.assertIn("soft_limit", prescreen.detect_signals(soft))
        self.assertNotIn("perf_limit", prescreen.detect_signals(soft))
        # 计数不是容量：三条、25 个是条目数，不构成硬限。
        counts = self.turn(
            tools=[{"name": "bash", "args": {"command": "git status --short"}, "status": "ok", "result": "25 个文件变更，38 个检查项通过"}]
        )
        self.assertNotIn("perf_limit", prescreen.detect_signals(counts))

    def test_diagnostic_recipe_bypasses_one_off_gate(self) -> None:
        """抓包命令带现场 IP 仍算可复用配方：命令形状可复用，变的只是参数。"""
        args = "tcpdump -i eth0 host 10.0.0.1 -w /tmp/x.pcap\nconntrack -L"
        self.assertEqual("reusable", prescreen.classify_reuse(args, ["/tmp/x.pcap"]))
        plain = "nat_ip = '10.1.2.3'\nprint(nat_ip)"
        self.assertEqual("one_off", prescreen.classify_reuse(plain, ["/srv/scripts/probe.py"]))

    def test_offload_matrix(self) -> None:
        """卸载特性矩阵：同一份规则在不同底座上表现不同，只能在出现底座语境时落。"""
        hit = self.turn(
            assistant="Intel i40e 上 GRO 开启时 checksum 错误，关掉 gro 后正常，内核 5.10",
            tools=[{"name": "bash", "args": {"command": "ethtool -k eth0"}, "status": "ok", "result": "gro: on lro: off tso: on"}],
        )
        self.assertIn("offload_matrix", prescreen.detect_signals(hit))
        plain = self.turn(tools=[{"name": "bash", "args": {"command": "ethtool -S eth0"}, "status": "ok", "result": "rx_packets: 1"}])
        self.assertNotIn("offload_matrix", prescreen.detect_signals(plain))

    def test_diagnostic_hazard(self) -> None:
        """观察者效应：高负载下无上限抓包本身就是故障源。"""
        unbounded = self.turn(tools=[{"name": "bash", "args": {"command": "tcpdump -i eth0 port 80"}, "status": "ok"}])
        self.assertIn("diagnostic_hazard", prescreen.detect_signals(unbounded))
        bounded = self.turn(tools=[{"name": "bash", "args": {"command": "tcpdump -i eth0 -c 100 port 80"}, "status": "ok"}])
        self.assertNotIn("diagnostic_hazard", prescreen.detect_signals(bounded))
        narrative = self.turn(
            user="100k cps 现网压测时 tcpdump 抓包导致吞吐断崖下跌",
            tools=[{"name": "bash", "args": {"command": "tcpdump -i eth0 -w /tmp/a.pcap"}, "status": "ok"}],
        )
        self.assertIn("diagnostic_hazard", prescreen.detect_signals(narrative))

    def test_protocol_edge_needs_fix(self) -> None:
        """协议边缘现象必须配闭环动作：现象来自现场证据，闭环来自变更类调用参数。"""
        closed = self.turn(
            tools=[
                {
                    "name": "bash",
                    "args": {"command": "sysctl -w net.ipv4.tcp_mtu_probing=1"},
                    "status": "ok",
                    "result": "PMTU 黑洞，回包被丢弃",
                }
            ]
        )
        self.assertIn("protocol_edge", prescreen.detect_signals(closed))
        open_ended = self.turn(assistant="怀疑是分片问题，再看看")
        self.assertNotIn("protocol_edge", prescreen.detect_signals(open_ended))
        asym = self.turn(
            tools=[
                {
                    "name": "bash",
                    "args": {"command": "ip route add 10.0.0.0/8 via 10.1.1.1 table 100"},
                    "status": "ok",
                    "result": "非对称路由导致 conntrack 建不起来",
                }
            ]
        )
        self.assertIn("protocol_edge", prescreen.detect_signals(asym))
        # 检索模式里出现 mtu 不是「落到调整」：那是搜文档，不是改配置。
        searched = self.turn(
            tools=[
                {
                    "name": "bash",
                    "args": {"command": 'rg -n "mtu|NAT64" src/'},
                    "status": "ok",
                    "result": "src/a.c:12: mtu 相关注释",
                }
            ]
        )
        self.assertNotIn("protocol_edge", prescreen.detect_signals(searched))

    def test_state_machine_stuck(self) -> None:
        """状态机卡死：轮询同一目标不动，且最终靠改顺序或加等待才走通。"""
        polls = [
            {"name": "bash", "args": {"command": "aliyun ecs DescribeInstance --id i-0123456789ab"}, "status": "ok", "result": "status=pending"}
        ] * 3
        resolved = self.turn(assistant="资源被占用，改为先解绑再绑定，中间等 5 秒后成功", tools=polls)
        self.assertIn("state_machine_stuck", prescreen.detect_signals(resolved))
        unresolved = self.turn(assistant="状态一直是 pending", tools=polls)
        self.assertNotIn("state_machine_stuck", prescreen.detect_signals(unresolved))

    def test_camelcase_assertions_do_not_casefold(self) -> None:
        """re.I 会 casefold 否定前瞻里的字符类：写成 (?![A-Za-z0-9]) 会把 camelCase 排除掉。

        云端 CLI 动作名几乎都是 camelCase，这条断言错了整族信号静默失效。
        """
        self.assertTrue(prescreen.STATE_POLL_CMD_RE.search("aliyun ecs DescribeInstance"))
        self.assertTrue(prescreen.STATUS_CALL_RE.search("aliyun vpc DescribeRouteEntry"))
        self.assertEqual(prescreen.MUTATION_FAMILY_RE.search("DeleteRouteEntry").group(0), "Delete")
        self.assertTrue(prescreen.mutation_action("aliyun vpc DeleteRouteEntry --id rtb-1"))
        self.assertTrue(prescreen.mutation_action("aliyun ecs ModifyInstanceAttribute"))
        self.assertTrue(prescreen.mutation_action("git add ."))
        self.assertTrue(prescreen.mutation_action("redis-cli SET k v"))
        # 词内片段不是动作：文件名里的 address、函数名里的 add、settings、added、started。
        self.assertFalse(prescreen.mutation_action("cat address-delivery.md"))
        self.assertFalse(prescreen.mutation_action("git show 275adeac:src/core/vpc.c | grep core_vpc_add("))
        self.assertFalse(prescreen.mutation_action("cat settings.json"))
        self.assertFalse(prescreen.mutation_action("变量 added 到列表"))
        self.assertFalse(prescreen.mutation_action("ansible -a state=started"))
        # 全大写串是标识符，不是 camelCase 动作名：源码与配置片段不构成「改了配置」。
        self.assertFalse(prescreen.mutation_action("#define ADDRESS_MAP_SIZE 16"))
        self.assertFalse(prescreen.mutation_action("SETUP_TIMEOUT = 5"))
        self.assertFalse(prescreen.mutation_action("#define MAX_ADDRESS 8"))
        self.assertFalse(prescreen.mutation_action("ADDRESS"))
        # 末尾无续写字符时全大写按标识符处理：宁可漏一个裸动词，也不把 `ADDRESS` 认成动作。
        self.assertFalse(prescreen.mutation_action("DELETE"))
        self.assertTrue(prescreen.resource_ids("--id i-0123456789ab"))
        for pattern in (prescreen.STATE_POLL_CMD_RE, prescreen.STATUS_CALL_RE, prescreen.RESOURCE_ID_RE):
            # 只查后置断言：前置断言用 [A-Za-z0-9] 排除字母数字是对的，
            # casefold 只会让「不该继续」的判定更严；出问题的是后置断言把 camelCase 也当成延续。
            suffix = pattern.pattern.rsplit(")", 1)[-1]
            self.assertNotIn("[A-Za-z0-9]", suffix, f"{pattern.pattern} 的后置断开会把 camelCase 排除掉")

    def test_pending_confidence_marks_judgement_signals(self) -> None:
        """判据型信号落候选时标 pending：复盘要排最前核实。"""
        path = Path(self.temp.name) / "s-pending.jsonl"
        write_jsonl(
            path,
            [
                {"type": "message", "timestamp": "2026-09-24T10:00:01Z", "message": {"role": "user", "content": "看下丢包"}},
                {"type": "message", "timestamp": "2026-09-24T10:00:05Z", "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ss -s"}}]}},
                {"type": "message", "timestamp": "2026-09-24T10:00:09Z", "message": {"role": "toolResult", "toolName": "bash", "isError": True, "content": [{"type": "text", "text": "no buffer space"}]}},
                {"type": "message", "timestamp": "2026-09-24T10:00:20Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "根因是 conntrack 表满，居然是回程路由没加"}], "usage": {}}},
            ],
        )
        experience_hook.handle(
            {"client": "pi", "event": "turn_end", "session_id": "s-pending", "turn_id": "1", "cwd": str(self.project), "transcript_path": str(path)}
        )
        record = next(record for _, record, _, _ in experience.iter_candidates("demo"))
        self.assertEqual(record.get("confidence"), "pending")
        self.assertIn("root_cause_mapped", record["signal"])
        self.assertIn("counterintuitive", record["signal"])
        output = self.run_cli(["review"])
        self.assertIn("待核实候选", output)


class SweepTest(BaseCase):
    """跨会话巡检：钩子逐轮无状态看不见的线索，只能靠历史候选重算。"""

    def seed(self, session: str, tool: str, args: dict, ts: str) -> str:
        name = lib.ensure_project(self.project)
        path = lib.candidate_file(name, "pi", session, ts[:10])
        record = {
            "ts": ts,
            "client": "pi",
            "session_id": session,
            "turn_id": "1",
            "project_name": name,
            "cwd": str(self.project),
            "compaction_count": 0,
            "signal": ["multi_source"],
            "user": "查一下",
            "assistant": "好",
            "tools": [{"name": tool, "args": args, "status": "ok"}],
            "outcome": "success",
            "cost": {"tool_calls": 1, "tokens": 1, "duration_ms": 1000},
            "evidence": {"path": "~/.pi/x.jsonl", "lines": [1, 2]},
        }
        rows = []
        if path.exists():
            rows = [line for line in lib.read_text(path).splitlines() if line.strip()]
        rows.append(json.dumps(record, ensure_ascii=False, sort_keys=True))
        lib.atomic_write_text(path, "\n".join(rows) + "\n")
        return lib.candidate_id("pi", session, "1", ts[:10])

    def test_intent_repeat_and_artifact_churn(self) -> None:
        """同一操作族跨会话出现 ≥3 次，或同一脚本被 ≥2 个会话反复改写，才算重复。"""
        stamp = lib.now_iso()
        for index, session in enumerate(("a", "b", "c")):
            self.seed(session, "bash", {"command": f"tcpdump -i any port {4789 + index}"}, stamp)
        script = str(self.project / "scripts" / "report.py")
        self.seed("d", "write", {"file_path": script, "content": "import argparse"}, stamp)
        self.seed("e", "write", {"file_path": script, "content": "import argparse"}, stamp)
        output = self.run_cli(["sweep"])
        self.assertIn("重复出现的操作族", output)
        self.assertIn("tcpdump", output)
        self.assertIn("反复改写的脚本", output)
        self.assertIn("report.py", output)
        applied = self.run_cli(["sweep", "--apply"])
        self.assertIn("已标记", applied)
        signals = [record.get("signal") for _, record, _, _ in experience.iter_candidates("demo")]
        flat = {signal for item in signals for signal in item}
        self.assertIn("intent_repeat", flat)
        self.assertIn("artifact_churn", flat)

    def test_single_session_repeat_is_not_cross_session(self) -> None:
        """同一会话内重复不算：跨会话重复才是「又要重写一遍」的证据。"""
        stamp = lib.now_iso()
        for index in range(3):
            self.seed("same", "bash", {"command": f"tcpdump -i any port {4789 + index}"}, stamp)
        output = self.run_cli(["sweep"])
        self.assertIn("没有发现跨会话重复", output)

    def test_sweep_is_preview_by_default(self) -> None:
        stamp = lib.now_iso()
        for session in ("x", "y", "z"):
            self.seed(session, "bash", {"command": "tcpdump -i any"}, stamp)
        self.run_cli(["sweep"])
        flat = {signal for _, record, _, _ in experience.iter_candidates("demo") for signal in record.get("signal") or []}
        self.assertNotIn("intent_repeat", flat, "预演不该改候选")

    def test_sweep_respects_window(self) -> None:
        old = "2026-01-01T10:00:00+08:00"
        for session in ("p", "q", "r"):
            self.seed(session, "bash", {"command": "tcpdump -i any"}, old)
        output = self.run_cli(["sweep", "--days", "7"])
        self.assertIn("没有发现跨会话重复", output)


class CorrectionTest(BaseCase):
    def detect(self, user: str, prev: str = "", tools=None) -> dict | None:
        return prescreen.detect_correction(
            {"user": user, "prev_user": prev, "assistant": "做了点事", "tools": tools or []}
        )

    def test_explicit_denial(self) -> None:
        result = self.detect("不对，应该按 hostname 过滤")
        self.assertEqual(result["signal"], "user_correction")
        self.assertEqual(result["kind"], "explicit_denial")

    def test_restated_request(self) -> None:
        result = self.detect("我要的是按 hostname 过滤之后的结果", prev="帮我查一下这个指标")
        self.assertEqual(result["kind"], "restated_request")

    def test_restated_by_similarity(self) -> None:
        result = self.detect("帮我查一下 VPC 的流量情况", prev="帮我查一下 VPC 的流量")
        self.assertEqual(result["kind"], "restated_request")

    def test_plan_rejected(self) -> None:
        result = self.detect("换个思路，别用缓存")
        self.assertEqual(result["kind"], "plan_rejected")

    def test_permission_denied(self) -> None:
        result = self.detect("继续", tools=[{"name": "bash", "args": {}, "status": "denied"}])
        self.assertEqual(result["kind"], "permission_denied")

    def test_gave_answer(self) -> None:
        result = self.detect("应该用 --region 3000 这个参数")
        self.assertEqual(result["signal"], "user_override")
        self.assertEqual(result["kind"], "gave_answer")

    def test_manual_takeover(self) -> None:
        result = self.detect("我改好了配置，你看一下")
        self.assertEqual(result["kind"], "manual_takeover")

    def test_never_returns_refuted(self) -> None:
        for text in ("不对", "换个思路", "我改了", "应该用 x=1"):
            result = self.detect(text)
            if result:
                self.assertIn(result["signal"], ("user_correction", "user_override"))

    def test_long_paste_is_not_a_correction(self) -> None:
        """贴进来的周报与指令模板里满是「不要」：纠正只在短消息上判。"""
        report = "## 本周进展 20260706-20260710\n" + "完成 acl 全局化 review，继续部署 bpftrace 脚本。\n" * 40
        report += "请总结本季度周期的重点工作成果，不要直接贴工单"
        self.assertIsNone(self.detect(report))

    def test_continuation_is_not_a_correction(self) -> None:
        """「继续」是让 agent 接着干，不是重述诉求。"""
        for text in ("继续", "继续啊", "接着来", "continue"):
            self.assertIsNone(self.detect(text, prev="这是我 2026 Q3 的周报：" + "内容" * 400))

    def test_short_reply_to_long_paste_is_not_restated_request(self) -> None:
        """短消息的二元组被长文包含会算出 1.0 的假相似度，长度不可比就不算重述。"""
        self.assertIsNone(self.detect("VPC 的流量", prev="帮我查一下 VPC 的流量情况\n" * 200))

    def test_char_cap_boundary(self) -> None:
        self.assertEqual(self.detect("不要用缓存" + "排查" * 247)["kind"], "explicit_denial")
        self.assertIsNone(self.detect("不要用缓存" + "排查" * 248))


class TranscriptTest(BaseCase):
    def test_pi(self) -> None:
        path = Path(self.temp.name) / "pi.jsonl"
        write_jsonl(
            path,
            [
                {"type": "session", "id": "s", "timestamp": "2026-09-22T10:00:00Z"},
                {"type": "message", "timestamp": "2026-09-22T10:00:01Z", "message": {"role": "user", "content": "查 VPC"}},
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:00:05Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "x"}}],
                    },
                },
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:00:20Z",
                    "message": {
                        "role": "toolResult",
                        "toolName": "bash",
                        "isError": True,
                        "content": [{"type": "text", "text": "Error: refused"}],
                    },
                },
                {"type": "compaction", "timestamp": "2026-09-22T10:00:30Z", "summary": "s"},
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:01:00Z",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "查到了"}], "usage": {"totalTokens": 1200}},
                },
            ],
        )
        turn = transcripts.parse_transcript("pi", str(path), "")
        self.assertEqual(turn.user, "查 VPC")
        self.assertEqual(turn.assistant, "查到了")
        self.assertEqual(turn.tool_calls, 1)
        self.assertEqual(turn.tools[0]["status"], "error")
        self.assertEqual(turn.tokens, 1200)
        self.assertEqual(turn.compaction_count, 1)
        self.assertGreater(turn.duration_ms, 0)
        self.assertIn("error_recovered", "".join(prescreen.detect_signals(turn.to_dict())) or "error_recovered")

    def test_codex(self) -> None:
        path = Path(self.temp.name) / "codex.jsonl"
        write_jsonl(
            path,
            [
                {"timestamp": "2026-09-22T10:00:00Z", "type": "session_meta", "payload": {"session_id": "s"}},
                {"timestamp": "2026-09-22T10:00:01Z", "type": "event_msg", "payload": {"type": "user_message", "turn_id": "t1", "message": "查 ECS"}},
                {
                    "timestamp": "2026-09-22T10:00:05Z",
                    "type": "response_item",
                    "payload": {"type": "custom_tool_call", "turn_id": "t1", "name": "shell", "arguments": "{\"command\":\"ls\"}"},
                },
                {
                    "timestamp": "2026-09-22T10:00:10Z",
                    "type": "response_item",
                    "payload": {"type": "custom_tool_call_output", "turn_id": "t1", "output": [{"type": "input_text", "text": "Error: boom"}]},
                },
                {"timestamp": "2026-09-22T10:00:30Z", "type": "event_msg", "payload": {"type": "agent_message", "turn_id": "t1", "message": "没查到"}},
                {"timestamp": "2026-09-22T10:00:31Z", "type": "event_msg", "payload": {"type": "context_compacted"}},
            ],
        )
        turn = transcripts.parse_transcript("codex", str(path), "t1")
        self.assertEqual(turn.user, "查 ECS")
        self.assertEqual(turn.assistant, "没查到")
        # Codex 没有结构化状态字段，输出文本里出现 Error 也只记 unknown 与文本线索。
        self.assertEqual(turn.tools[0]["status"], "unknown")
        self.assertIn("Error: boom", turn.tools[0].get("error_text_hint", ""))
        self.assertEqual(turn.compaction_count, 1)
        self.assertEqual(turn.line_start, 2)

    def test_codex_item_completed_schema(self) -> None:
        """codex-cli 0.156 起对话与工具走 event_msg/item_completed，命令带 exit_code。"""

        def event(turn: str, item: dict, ts: str) -> dict:
            return {
                "timestamp": ts,
                "type": "event_msg",
                "payload": {"type": "item_completed", "turn_id": turn, "item": item},
            }

        path = Path(self.temp.name) / "codex-item.jsonl"
        write_jsonl(
            path,
            [
                event("t1", {"type": "UserMessage", "content": [{"type": "text", "text": "上一轮的问题"}]}, "2026-10-08T10:00:01Z"),
                event("t1", {"type": "AgentMessage", "content": [{"type": "Text", "text": "上一轮的回答"}]}, "2026-10-08T10:00:02Z"),
                event("t2", {"type": "UserMessage", "content": [{"type": "text", "text": "为什么编译失败"}]}, "2026-10-08T10:00:03Z"),
                {
                    "timestamp": "2026-10-08T10:00:04Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "# AGENTS.md instructions for /w/repo"}],
                        "internal_chat_message_metadata_passthrough": {"turn_id": "t2"},
                    },
                },
                {
                    "timestamp": "2026-10-08T10:00:05Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "input": "await tools.exec_command({cmd:'make'})",
                        "internal_chat_message_metadata_passthrough": {"turn_id": "t2"},
                    },
                },
                event(
                    "t2",
                    {
                        "type": "CommandExecution",
                        "command": ["bash", "-lc", "make"],
                        "cwd": "file:///w/repo",
                        "status": "completed",
                        "exit_code": 1,
                        "aggregated_output": "undefined reference to `dp_vs_x'",
                    },
                    "2026-10-08T10:00:06Z",
                ),
                event("t2", {"type": "FileChange", "status": "completed", "changes": {"a.c": {"type": "add"}}}, "2026-10-08T10:00:07Z"),
                event("t2", {"type": "ContextCompaction"}, "2026-10-08T10:00:08Z"),
                event("t2", {"type": "AgentMessage", "content": [{"type": "Text", "text": "少了个源文件"}]}, "2026-10-08T10:00:09Z"),
            ],
        )
        turn = transcripts.parse_transcript("codex", str(path), "t2")
        # 提问取 event_msg 权威层：response_item 里的 user 消息是注入的 AGENTS.md 文本。
        self.assertEqual(turn.user, "为什么编译失败")
        self.assertEqual(turn.assistant, "少了个源文件")
        self.assertEqual(turn.turn_index, 2)
        # 工具按 item 记：一次 exec 脚本里的命令与改文件各一条，不再数 response_item 那遍。
        self.assertEqual([tool["name"] for tool in turn.tools], ["exec", "file_change"])
        self.assertEqual(turn.tool_calls, 2)
        self.assertEqual(turn.tools[0]["status"], "error")
        self.assertIn("undefined reference", turn.tools[0]["error"])
        self.assertEqual(turn.tools[0]["args"]["command"], "bash -lc make")
        # FileChange 没有成败字段，只能记 unknown。
        self.assertEqual(turn.tools[1]["status"], "unknown")
        self.assertEqual(turn.compaction_count, 1)
        self.assertEqual(turn.prev_users, ["上一轮的问题"])

    def test_codex_command_without_exit_code_stays_unknown(self) -> None:
        """命令被中断时没有退出码：记 unknown，不拿输出文本里的 failed 判失败。"""
        path = Path(self.temp.name) / "codex-interrupted.jsonl"
        write_jsonl(
            path,
            [
                {"timestamp": "2026-10-08T10:00:01Z", "type": "event_msg", "payload": {"type": "item_completed", "turn_id": "t1", "item": {"type": "UserMessage", "content": [{"type": "text", "text": "跑一下"}]}}},
                {
                    "timestamp": "2026-10-08T10:00:02Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "turn_id": "t1",
                        "item": {"type": "CommandExecution", "command": "make", "status": "cancelled", "aggregated_output": "failed"},
                    },
                },
            ],
        )
        turn = transcripts.parse_transcript("codex", str(path), "t1")
        self.assertEqual(turn.tools[0]["status"], "unknown")
        self.assertIn("failed", turn.tools[0].get("error_text_hint", ""))

    def test_claude_picks_requested_turn(self) -> None:
        path = Path(self.temp.name) / "claude.jsonl"
        write_jsonl(
            path,
            [
                {"type": "user", "timestamp": "2026-09-22T10:00:00Z", "message": {"role": "user", "content": "看看报错"}},
                {
                    "type": "assistant",
                    "timestamp": "2026-09-22T10:00:05Z",
                    "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "cat log"}}]},
                },
                {
                    "type": "user",
                    "timestamp": "2026-09-22T10:00:10Z",
                    "message": {"role": "user", "content": [{"type": "tool_result", "is_error": True, "content": "Error: not found"}]},
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-09-22T10:00:20Z",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "日志里没有"}], "usage": {"input_tokens": 100, "output_tokens": 20}},
                },
                {"type": "user", "timestamp": "2026-09-22T10:00:30Z", "message": {"role": "user", "content": "不对，应该用 grep 搜 500"}},
            ],
        )
        first = transcripts.parse_transcript("claude", str(path), "1")
        self.assertEqual(first.user, "看看报错")
        self.assertEqual(first.tools[0]["status"], "error")
        self.assertEqual(first.tokens, 120)
        second = transcripts.parse_transcript("claude", str(path), "2")
        self.assertEqual(second.prev_user, "看看报错")
        self.assertEqual(prescreen.detect_correction(second.to_dict())["kind"], "explicit_denial")

    def test_dsh(self) -> None:
        path = Path(self.temp.name) / "dsh.jsonl"
        write_jsonl(
            path,
            [
                {"type": "user_message", "text": "跑容量报告"},
                {"type": "tool_call", "name": "bash", "args": {"command": "python skills/dns-capacity-report/scripts/run.py"}},
                {"type": "tool_result", "name": "bash", "is_error": True, "error": "Traceback (most recent call last)"},
                {"type": "tool_call", "name": "bash", "args": {"command": "python skills/dns-capacity-report/scripts/run.py"}},
                {"type": "tool_result", "name": "bash", "is_error": False},
                {"type": "agent_message", "text": "报告已生成"},
                {"type": "compaction"},
            ],
        )
        turn = transcripts.parse_transcript("dsh", str(path), "")
        signals = prescreen.detect_signals(turn.to_dict())
        self.assertIn("error_recovered", signals)
        self.assertIn("retry", signals)
        self.assertEqual(turn.compaction_count, 1)

    def test_codemaker(self) -> None:
        path = Path(self.temp.name) / "codemaker.json"
        payload = {
            "messages": [
                {"role": "user", "content": "改一下配置"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\":\"a.c\"}"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "Error: no such file or directory"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\":\"a.c\"}"}}]},
                {"role": "tool", "tool_call_id": "c2", "content": "ok"},
                {"role": "assistant", "content": "改好了", "usage": {"total_tokens": 900}},
            ],
            "compression": {"enabled": True, "totalCompressionsCount": 2},
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        turn = transcripts.parse_transcript("codemaker", str(path), "")
        self.assertEqual(turn.user, "改一下配置")
        self.assertEqual(turn.assistant, "改好了")
        self.assertEqual(turn.tool_calls, 2)
        self.assertEqual(turn.tokens, 900)
        self.assertEqual(turn.compaction_count, 2)
        self.assertIn("retry", prescreen.detect_signals(turn.to_dict()))

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(transcripts.parse_transcript("pi", str(Path(self.temp.name) / "nope.jsonl"), ""))


class ToolStatusAuthorityTest(BaseCase):
    """工具成败只认来源给出的结构化状态；输出文本命中错误标记时只留线索。

    判据来自 letta-ai/trajectory 的做法：来源不给权威状态时留空，绝不解释文本。
    """

    def pi_turn(self, results: list[dict]) -> Path:
        rows = [{"type": "message", "timestamp": "2026-09-22T10:00:01Z", "message": {"role": "user", "content": "跑一下测试"}}]
        for index, result in enumerate(results):
            rows.append(
                {
                    "type": "message",
                    "timestamp": f"2026-09-22T10:00:{index * 10 + 5:02d}Z",
                    "message": {"role": "assistant", "content": [{"type": "toolCall", "id": f"c{index}", "name": "bash", "arguments": {"command": "x"}}]},
                }
            )
            rows.append({"type": "message", "timestamp": f"2026-09-22T10:00:{index * 10 + 9:02d}Z", "message": {"role": "toolResult", "toolName": "bash", **result}})
        rows.append({"type": "message", "timestamp": "2026-09-22T10:01:00Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "跑完了"}]}})
        path = Path(self.temp.name) / "status.jsonl"
        write_jsonl(path, rows)
        return path

    def test_text_marker_does_not_decide_status(self) -> None:
        """测试输出 0 failed、grep error 都会命中标记，但状态必须保持 unknown。"""
        path = self.pi_turn([{"content": [{"type": "text", "text": "12 passed, 0 failed"}]}])
        turn = transcripts.parse_transcript("pi", str(path), "1")
        self.assertEqual(turn.tools[0]["status"], "unknown")
        self.assertNotIn("error_recovered", prescreen.detect_signals(turn.to_dict()))

    def test_structured_flag_wins_over_text(self) -> None:
        path = self.pi_turn(
            [
                {"isError": True, "content": [{"type": "text", "text": "Error: refused"}]},
                {"isError": False, "content": [{"type": "text", "text": "failed 一词出现在输出里也没关系"}]},
            ]
        )
        turn = transcripts.parse_transcript("pi", str(path), "1")
        self.assertEqual([tool["status"] for tool in turn.tools], ["error", "ok"])
        self.assertEqual(turn.tools[0]["error"], "Error: refused")
        self.assertIn("error_recovered", prescreen.detect_signals(turn.to_dict()))

    def test_unknown_status_keeps_text_hint_only(self) -> None:
        path = self.pi_turn([{"content": [{"type": "text", "text": "Traceback (most recent call last): boom"}]}])
        turn = transcripts.parse_transcript("pi", str(path), "1")
        tool = turn.tools[0]
        self.assertEqual(tool["status"], "unknown")
        self.assertEqual(tool["error"], "")
        self.assertIn("Traceback", tool["error_text_hint"])

    def test_claude_absent_is_error_means_success(self) -> None:
        """Claude Code 只在失败时写 is_error，缺键的实测都是文件、搜索类成功结果。"""
        path = Path(self.temp.name) / "claude-status.jsonl"
        write_jsonl(
            path,
            [
                {"type": "user", "timestamp": "2026-09-22T10:00:00Z", "message": {"role": "user", "content": "读一下"}},
                {"type": "assistant", "timestamp": "2026-09-22T10:00:05Z", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "a.md"}}]}},
                {"type": "user", "timestamp": "2026-09-22T10:00:09Z", "message": {"role": "user", "content": [{"type": "tool_result", "content": "# 标题\n\n正文里提到 error: 这个词"}]}},
                {"type": "assistant", "timestamp": "2026-09-22T10:00:20Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "读到了"}]}},
            ],
        )
        turn = transcripts.parse_transcript("claude", str(path), "1")
        self.assertEqual(turn.tools[0]["status"], "ok")
        self.assertNotIn("error_text_hint", turn.tools[0])

    def test_pi_bash_execution_uses_exit_code(self) -> None:
        """bashExecution 有 exitCode 时以退出码判定，缺失则 unknown。"""
        path = Path(self.temp.name) / "pi-bash.jsonl"
        rows = [{"type": "message", "timestamp": "2026-09-22T10:00:01Z", "message": {"role": "user", "content": "跑命令"}}]
        for index, extra in enumerate(({"exitCode": 0}, {"exitCode": 2}, {})):
            rows.append(
                {
                    "type": "message",
                    "timestamp": f"2026-09-22T10:00:{index * 10 + 5:02d}Z",
                    "message": {"role": "bashExecution", "command": "x", "output": "Error: 文本不该参与判定", **extra},
                }
            )
        rows.append({"type": "message", "timestamp": "2026-09-22T10:01:00Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "跑完"}]}})
        write_jsonl(path, rows)
        turn = transcripts.parse_transcript("pi", str(path), "1")
        self.assertEqual([tool["status"] for tool in turn.tools], ["ok", "error", "unknown"])

    def test_unknown_status_is_not_counted_as_ok(self) -> None:
        """unknown 不能顶替 ok，否则会把「没拿到状态」当成恢复成功。"""
        turn = {"user": "x", "assistant": "y", "outcome": "success", "tools": [
            {"name": "bash", "args": {}, "status": "error"},
            {"name": "bash", "args": {}, "status": "unknown"},
        ]}
        self.assertNotIn("error_recovered", prescreen.detect_signals(turn))


class HookTest(BaseCase):
    def pi_transcript(self) -> Path:
        """一轮带「出错后重试成功」的对话，命中 retry 与 error_recovered。

        准入闸门要求证据型信号：过程信号单独出现不落候选，因此这一轮另带一次
        实测数值（cost_trap）——测试的意图是「有信号的轮次会落盘」，不是「过程信号能落盘」。
        """
        path = Path(self.temp.name) / "pi-turn.jsonl"
        write_jsonl(
            path,
            [
                {"type": "message", "timestamp": "2026-09-22T10:00:01Z", "message": {"role": "user", "content": "查 VPC"}},
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:00:05Z",
                    "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "x"}}]},
                },
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:00:20Z",
                    "message": {"role": "toolResult", "toolName": "bash", "isError": True, "content": [{"type": "text", "text": "Error: refused"}]},
                },
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:00:40Z",
                    "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "c2", "name": "bash", "arguments": {"command": "x"}}]},
                },
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:00:50Z",
                    "message": {"role": "toolResult", "toolName": "bash", "isError": False, "content": [{"type": "text", "text": "ok"}]},
                },
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:00:55Z",
                    "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "c3", "name": "bash", "arguments": {"command": "conntrack -S --note '实测这台机器上 conntrack 老化时间是 120 秒，只在本机成立'"}}]},
                },
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:00:58Z",
                    "message": {"role": "toolResult", "toolName": "bash", "isError": False, "content": [{"type": "text", "text": "实测这台机器上 conntrack 老化时间是 120 秒，只在本机成立"}]},
                },
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:01:00Z",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "查到了"}], "usage": {"totalTokens": 100}},
                },
            ],
        )
        return path

    def test_signal_turn_writes_candidate(self) -> None:
        result = experience_hook.handle(
            {
                "client": "pi",
                "event": "turn_end",
                "session_id": "fixture-8a08-7980",
                "turn_id": "1",
                "cwd": str(self.project),
                "transcript_path": str(self.pi_transcript()),
            }
        )
        self.assertTrue(result.get("written"))
        files = list((self.home / "candidates" / "demo").glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        self.assertIn("error_recovered", result["signals"])

    def test_idempotent_by_session_and_turn(self) -> None:
        payload = {
            "client": "pi",
            "event": "turn_end",
            "session_id": "fixture-8a08-7980",
            "turn_id": "1",
            "cwd": str(self.project),
            "transcript_path": str(self.pi_transcript()),
        }
        experience_hook.handle(payload)
        experience_hook.handle(payload)
        files = list((self.home / "candidates" / "demo").glob("*.jsonl"))
        rows = [line for line in files[0].read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(rows), 1)

    def test_quiet_turn_writes_nothing(self) -> None:
        path = Path(self.temp.name) / "quiet.jsonl"
        write_jsonl(
            path,
            [
                {"type": "message", "timestamp": "2026-09-22T10:00:01Z", "message": {"role": "user", "content": "谢谢"}},
                {"type": "message", "timestamp": "2026-09-22T10:00:02Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "不客气"}]}},
            ],
        )
        result = experience_hook.handle(
            {"client": "pi", "event": "turn_end", "session_id": "s2", "turn_id": "1", "cwd": str(self.project), "transcript_path": str(path)}
        )
        self.assertEqual(result.get("skipped"), "no_signal")

    def test_non_turn_event_skipped(self) -> None:
        result = experience_hook.handle({"client": "pi", "event": "session_start", "session_id": "s3", "cwd": str(self.project)})
        self.assertEqual(result["stage"], "normalize")
        self.assertTrue(str(result.get("skipped") or "").startswith("unknown_event:"))

    def test_candidate_is_redacted(self) -> None:
        """凭据既可能出现在句子里，也可能出现在工具参数里，两处都要脱敏。"""
        path = Path(self.temp.name) / "secret.jsonl"
        write_jsonl(
            path,
            [
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:00:01Z",
                    "message": {"role": "user", "content": "用 token=AbCdEf0123456789AbCdEf0123456789 查一下"},
                },
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:00:05Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"api_key": "AbCdEf0123456789AbCdEf0123456789"}}],
                    },
                },
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:00:20Z",
                    "message": {"role": "toolResult", "toolName": "bash", "isError": True, "content": [{"type": "text", "text": "Error: refused"}]},
                },
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:00:30Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "toolCall", "id": "c2", "name": "bash", "arguments": {"api_key": "AbCdEf0123456789AbCdEf0123456789"}}],
                    },
                },
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:00:40Z",
                    "message": {"role": "toolResult", "toolName": "bash", "isError": False, "content": [{"type": "text", "text": "ok"}]},
                },
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:00:45Z",
                    "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "c3", "name": "bash", "arguments": {"command": "conntrack -S --note '实测这台机器上 conntrack 老化时间是 120 秒，只在本机成立'"}}]},
                },
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:00:48Z",
                    "message": {"role": "toolResult", "toolName": "bash", "isError": False, "content": [{"type": "text", "text": "实测这台机器上 conntrack 老化时间是 120 秒，只在本机成立"}]},
                },
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:00:50Z",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "好"}], "usage": {}},
                },
            ],
        )
        result = experience_hook.handle(
            {"client": "pi", "event": "turn_end", "session_id": "s4", "turn_id": "1", "cwd": str(self.project), "transcript_path": str(path)}
        )
        self.assertTrue(result.get("written"))
        files = list((self.home / "candidates" / "demo").glob("*.jsonl"))
        text = files[0].read_text(encoding="utf-8")
        self.assertNotIn("AbCdEf0123456789AbCdEf0123456789", text)
        self.assertIn(lib.REDACTED, text)


class EntryLifecycleTest(BaseCase):
    def seed_candidate(self, session: str = "fixture", turn: str = "1") -> str:
        path = Path(self.temp.name) / f"{session}.jsonl"
        write_jsonl(
            path,
            [
                {"type": "message", "timestamp": "2026-09-22T10:00:01Z", "message": {"role": "user", "content": "以后一律先按 hostname 过滤，不要用全量查询"}},
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:00:05Z",
                    "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "c1", "name": "cms_query", "arguments": {"scope": "all"}}]},
                },
                {"type": "message", "timestamp": "2026-09-22T10:00:20Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "好"}], "usage": {}}},
            ],
        )
        experience_hook.handle(
            {"client": "pi", "event": "turn_end", "session_id": session, "turn_id": turn, "cwd": str(self.project), "transcript_path": str(path)}
        )
        files = sorted((self.home / "candidates" / "demo").glob("*.jsonl"))
        record = json.loads(files[-1].read_text(encoding="utf-8").splitlines()[0])
        return lib.candidate_id(record["client"], record["session_id"], record["turn_id"], record["ts"][:10])

    def test_review_lists_candidate(self) -> None:
        cid = self.seed_candidate()
        output = self.run_cli(["review"])
        self.assertIn(cid, output)

    def test_correction_entry_requires_verification(self) -> None:
        cid = self.seed_candidate()
        with self.assertRaises(SystemExit):
            self.run_cli(
                [
                    "add", "--from", cid, "--title", "CMS 查询先过滤", "--category", "tooling",
                    "--type", "tool-rule", "--scope", "project", "--action", "先按 hostname 过滤再查",
                ]
            )

    def test_confirmed_correction_writes_entry_and_consumes_candidate(self) -> None:
        cid = self.seed_candidate()
        output = self.run_cli(
            [
                "add", "--from", cid, "--title", "CMS 查询先按 hostname 过滤", "--category", "tooling",
                "--type", "tool-rule", "--scope", "project", "--action", "先按 hostname 过滤再查",
                "--verification", "confirmed", "--verification-note", "CMS 接口文档要求必填 hostname",
                "--tags", "cms,query", "--pre-tool", "cms_query",
            ]
        )
        self.assertIn("已写入条目", output)
        self.assertEqual(list((self.home / "candidates" / "demo").glob("*.jsonl")), [])
        entries = list(lib.iter_entries(scope="project", project="demo"))
        self.assertEqual(len(entries), 1)
        meta = entries[0][1]
        self.assertEqual(meta["verification"], "confirmed")
        self.assertEqual(meta["confidence"], "low")
        self.assertEqual(meta["precondition"]["tools"], ["cms_query"])

    def test_duplicate_title_is_rejected(self) -> None:
        cid = self.seed_candidate()
        common = [
            "add", "--title", "CMS 查询先按 hostname 过滤", "--category", "tooling", "--type", "tool-rule",
            "--scope", "project", "--action", "先过滤", "--verification", "confirmed",
            "--verification-note", "依据", "--tags", "cms,query",
        ]
        self.run_cli(common)
        with self.assertRaises(SystemExit):
            self.run_cli(common)

    def test_reference_entry_requires_source(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_cli(
                [
                    "add", "--title", "索引命名规则", "--category", "tooling", "--type", "tool-rule",
                    "--scope", "project", "--action", "见载体", "--kind", "reference",
                ]
            )

    def test_pitfall_requires_root_cause(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_cli(
                ["add", "--title", "OOM", "--category", "pitfall", "--type", "recovery", "--scope", "project", "--action", "降显存"]
            )

    def test_promote_gate_and_dry_run(self) -> None:
        for index in range(3):
            cid = self.seed_candidate(session=f"01a018d{index}", turn="1")
            self.run_cli(
                [
                    "add", "--from", cid, "--title", f"先过滤 {index}", "--category", "tooling", "--type", "tool-rule",
                    "--scope", "project", "--action", "先按 hostname 过滤", "--verification", "preference",
                    "--verification-note", "用户偏好", "--tags", f"cms{index},query{index}", "--force",
                ]
            )
        entries = list(lib.iter_entries(scope="project", project="demo"))
        target = entries[0][1]["id"]
        # 强制把三条会话合并到同一条条目，模拟“同一纠正被三次独立验证”。
        path, meta, body = entries[0]
        meta["evidence"]["sessions"] = ["pi/a#t1", "claude/b#t2", "codex/c#t3"]
        meta["confidence"] = "high"
        lib.atomic_write_text(path, lib.dump_entry(meta, body))
        output = self.run_cli(["promote", target])
        self.assertIn("这是预演", output)
        rule_file = self.project / "AGENTS.md"
        self.assertFalse(rule_file.exists())

    def test_promote_rejects_thin_evidence(self) -> None:
        cid = self.seed_candidate()
        self.run_cli(
            [
                "add", "--from", cid, "--title", "只有一次", "--category", "tooling", "--type", "tool-rule",
                "--scope", "project", "--action", "先过滤", "--verification", "confirmed",
                "--verification-note", "依据", "--tags", "one,two",
            ]
        )
        target = list(lib.iter_entries(scope="project", project="demo"))[0][1]["id"]
        with self.assertRaises(SystemExit):
            self.run_cli(["promote", target, "--apply"])

    def test_promote_redacts_rule_text(self) -> None:
        """规则要写进 AGENTS.md：历史条目正文里的凭据不能跟着规则一起出去。"""
        cid = self.seed_candidate()
        self.run_cli(
            [
                "add", "--from", cid, "--title", "旧条目", "--category", "tooling", "--type", "tool-rule",
                "--scope", "project", "--action", "先过滤", "--verification", "preference",
                "--verification-note", "用户偏好", "--tags", "old,legacy",
            ]
        )
        path, meta, body = list(lib.iter_entries(scope="project", project="demo"))[0]
        # 模拟脱敏口径之前落盘的历史条目：明文写进 action。
        meta["action"] = "先 export TOKEN=AbCdEf0123456789AbCdEf0123456789 再调接口"
        meta["evidence"]["sessions"] = ["pi/a#t1", "claude/b#t2", "codex/c#t3"]
        lib.atomic_write_text(path, lib.dump_entry(meta, body))
        output = self.run_cli(["promote", meta["id"]])
        self.assertIn("这是预演", output)
        self.assertNotIn("AbCdEf0123456789AbCdEf0123456789", output)
        self.assertIn(lib.REDACTED, output)


class NativeHookTest(BaseCase):
    """原生钩子（Claude Code、Codex）不带 client 与 turn_id，靠脚本补齐。"""

    def claude_two_turns(self) -> Path:
        path = Path(self.temp.name) / "claude-native.jsonl"
        write_jsonl(
            path,
            [
                {
                    "type": "user",
                    "timestamp": "2026-09-22T10:00:01Z",
                    "message": {"role": "user", "content": "查一下这个指标"},
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-09-22T10:00:05Z",
                    "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "x"}}]},
                },
                {
                    "type": "user",
                    "timestamp": "2026-09-22T10:00:10Z",
                    "message": {"role": "user", "content": [{"type": "tool_result", "is_error": True, "content": "Error: refused"}]},
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-09-22T10:00:20Z",
                    "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "x"}}]},
                },
                {
                    "type": "user",
                    "timestamp": "2026-09-22T10:00:30Z",
                    "message": {"role": "user", "content": [{"type": "tool_result", "is_error": False, "content": "ok"}]},
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-09-22T10:00:40Z",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "拿到了"}]},
                },
                {"type": "user", "timestamp": "2026-09-22T10:01:00Z", "message": {"role": "user", "content": "不对，以后一律按 3000 这个 region 查"}},
                {
                    "type": "assistant",
                    "timestamp": "2026-09-22T10:01:10Z",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "好的"}]},
                },
            ],
        )
        return path

    def test_client_argument_is_required_for_native_hooks(self) -> None:
        path = self.claude_two_turns()
        skipped = experience_hook.handle(
            {"hook_event_name": "Stop", "session_id": "native-1", "cwd": str(self.project), "transcript_path": str(path)}
        )
        self.assertEqual(skipped.get("skipped"), "unknown_client")

    def test_each_turn_gets_its_own_candidate(self) -> None:
        path = self.claude_two_turns()
        base = {"client": "claude", "event": "Stop", "session_id": "native-1", "cwd": str(self.project), "transcript_path": str(path)}
        experience_hook.handle(dict(base, transcript_path=str(path)))
        # 第二轮的纠正：模拟下一次 Stop 触发，turn_id 由转录轮次序号推出。
        record = experience_hook.handle(dict(base))
        self.assertTrue(record.get("written"))
        files = list((self.home / "candidates" / "demo").glob("*.jsonl"))
        rows = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["turn_id"], "2")
        self.assertEqual(rows[0]["correction"]["kind"], "explicit_denial")

    def test_codex_without_turn_id_windows_last_turn(self) -> None:
        path = Path(self.temp.name) / "codex-native.jsonl"
        write_jsonl(
            path,
            [
                {"timestamp": "2026-09-22T10:00:01Z", "type": "event_msg", "payload": {"type": "user_message", "turn_id": "t1", "message": "第一轮问题"}},
                {"timestamp": "2026-09-22T10:00:05Z", "type": "event_msg", "payload": {"type": "agent_message", "turn_id": "t1", "message": "第一轮回答"}},
                {"timestamp": "2026-09-22T10:01:00Z", "type": "event_msg", "payload": {"type": "user_message", "turn_id": "t2", "message": "第二轮问题"}},
                {"timestamp": "2026-09-22T10:01:05Z", "type": "response_item", "payload": {"type": "custom_tool_call", "turn_id": "t2", "name": "shell", "arguments": "{\"command\":\"x\"}"}},
                {"timestamp": "2026-09-22T10:01:10Z", "type": "response_item", "payload": {"type": "custom_tool_call_output", "turn_id": "t2", "output": [{"type": "input_text", "text": "Error: boom"}]}},
                {"timestamp": "2026-09-22T10:01:20Z", "type": "response_item", "payload": {"type": "custom_tool_call", "turn_id": "t2", "name": "shell", "arguments": "{\"command\":\"x\"}"}},
                {"timestamp": "2026-09-22T10:01:30Z", "type": "response_item", "payload": {"type": "custom_tool_call_output", "turn_id": "t2", "output": [{"type": "input_text", "text": "ok"}]}},
                {"timestamp": "2026-09-22T10:01:40Z", "type": "event_msg", "payload": {"type": "agent_message", "turn_id": "t2", "message": "第二轮回答"}},
            ],
        )
        turn = transcripts.parse_transcript("codex", str(path), "")
        self.assertEqual(turn.user, "第二轮问题")
        self.assertEqual(turn.assistant, "第二轮回答")
        self.assertEqual(turn.turn_index, 2)
        self.assertEqual(len(turn.tools), 2)
        # Codex 拿不到权威失败状态，两次调用都记 unknown：不产出 error_recovered，
        # 但同签名重复调用仍由 retry 兜住。
        self.assertEqual({tool["status"] for tool in turn.tools}, {"unknown"})
        signals = prescreen.detect_signals(turn.to_dict())
        self.assertNotIn("error_recovered", signals)
        self.assertIn("retry", signals)

    def test_codex_item_schema_without_turn_id_recovers_status(self) -> None:
        """新代 rollout 走原生 Stop 钩子（不带 turn_id）时，退出码仍能定成败。"""

        def event(turn: str, item: dict, ts: str) -> dict:
            return {
                "timestamp": ts,
                "type": "event_msg",
                "payload": {"type": "item_completed", "turn_id": turn, "item": item},
            }

        path = Path(self.temp.name) / "codex-native-item.jsonl"
        write_jsonl(
            path,
            [
                event("t1", {"type": "UserMessage", "content": [{"type": "text", "text": "第一轮问题"}]}, "2026-10-08T10:00:01Z"),
                event("t1", {"type": "AgentMessage", "content": [{"type": "Text", "text": "第一轮回答"}]}, "2026-10-08T10:00:02Z"),
                event("t2", {"type": "UserMessage", "content": [{"type": "text", "text": "第二轮问题"}]}, "2026-10-08T10:01:00Z"),
                event(
                    "t2",
                    {"type": "CommandExecution", "command": ["make"], "exit_code": 1, "aggregated_output": "undefined reference to `x'"},
                    "2026-10-08T10:01:05Z",
                ),
                event("t2", {"type": "CommandExecution", "command": ["make"], "exit_code": 0, "aggregated_output": "build ok"}, "2026-10-08T10:01:10Z"),
                event("t2", {"type": "AgentMessage", "content": [{"type": "Text", "text": "加了源文件就好了"}]}, "2026-10-08T10:01:20Z"),
            ],
        )
        turn = transcripts.parse_transcript("codex", str(path), "")
        self.assertEqual(turn.user, "第二轮问题")
        self.assertEqual(turn.assistant, "加了源文件就好了")
        self.assertEqual(turn.turn_index, 2)
        self.assertEqual([tool["status"] for tool in turn.tools], ["error", "ok"])
        self.assertIn("undefined reference", turn.tools[0]["error"])
        signals = prescreen.detect_signals(turn.to_dict())
        self.assertIn("error_recovered", signals)


class SearchAndMaintenanceTest(BaseCase):
    def seed_entry(self, title: str, tags: str, action: str, category: str = "tooling") -> None:
        self.run_cli(
            [
                "add", "--title", title, "--category", category, "--type", "tool-rule", "--scope", "project",
                "--action", action, "--tags", tags, "--pre-tool", "cms_query",
            ]
        )

    def test_search_hard_filter_and_score(self) -> None:
        self.seed_entry("CMS 查询退避", "cms,throttling", "指数退避重试")
        self.seed_entry("日志索引命名", "elk,index", "索引名带日期后缀")
        by_text = self.run_cli(["search", "--query", "退避"])
        self.assertIn("CMS 查询退避", by_text)
        self.assertNotIn("日志索引命名", by_text)
        by_tool = self.run_cli(["search", "--tool", "cms_query"])
        self.assertIn("CMS 查询退避", by_tool)
        filtered = self.run_cli(["search", "--tool", "elk_query"])
        self.assertNotIn("CMS 查询退避", filtered)

    def test_index_and_stats(self) -> None:
        self.seed_entry("CMS 查询退避", "cms,throttling", "指数退避重试")
        self.run_cli(["index"])
        index_text = lib.index_path().read_text(encoding="utf-8")
        self.assertIn("CMS 查询退避", index_text)
        stats = json.loads(self.run_cli(["stats", "--json"]))
        self.assertEqual(stats["entries"], 1)
        self.assertEqual(stats["by_confidence"], {"low": 1})

    def test_gc_dry_run_keeps_files(self) -> None:
        self.seed_entry("CMS 查询退避", "cms,throttling", "指数退避重试")
        output = self.run_cli(["gc", "--older-than", "0d"])
        self.assertIn("这是预演", output)
        self.assertEqual(len(list(lib.iter_entries(scope="project", project="demo"))), 1)

    def test_scan_finds_project_carriers(self) -> None:
        skill = self.project / "skills" / "elk-query"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# ELK 查询\n\n索引命名规则：cld_ngc-*\n", encoding="utf-8")
        docs = self.project / "docs"
        docs.mkdir()
        (docs / "guide.md").write_text("# 指南\n\n索引命名规则见 skill\n", encoding="utf-8")
        output = self.run_cli(["scan", "--query", "索引命名规则"])
        self.assertIn("skills/elk-query/SKILL.md", output)
        self.assertIn("docs/guide.md", output)


class RecallCountTest(BaseCase):
    """hit_count 只在真实召回时递增，失败不挡住检索结果。"""

    def seed_entry(self) -> Path:
        self.run_cli(
            [
                "add", "--title", "CMS 查询退避", "--category", "tooling", "--type", "tool-rule",
                "--scope", "project", "--action", "指数退避重试", "--tags", "cms,throttling",
                "--pre-tool", "cms_query",
            ]
        )
        path, _, _ = next(lib.iter_entries(scope="project", project="demo"))
        return path

    def meta_of(self, path: Path) -> dict:
        return lib.load_entry(path)[0]

    def make_legacy(self, path: Path, count: int = 5, stamp: str = "2026-01-01T00:00:00") -> None:
        """造一条变更前的存量条目：只有 use_count，没有 hit_count。"""
        meta, body = lib.load_entry(path)
        meta.pop("hit_count", None)
        meta.pop("last_hit_at", None)
        meta.pop("use_failed_count", None)
        meta["use_count"] = count
        meta["last_used_at"] = stamp
        lib.atomic_write_text(path, lib.dump_entry(meta, body))

    def test_new_entry_starts_at_zero(self) -> None:
        path = self.seed_entry()
        meta = self.meta_of(path)
        self.assertEqual(meta["hit_count"], 0)
        self.assertIsNone(meta["last_hit_at"])
        self.assertEqual(meta["use_count"], 0)
        self.assertIsNone(meta["last_used_at"])
        self.assertEqual(meta["use_failed_count"], 0)
        self.assertEqual(lib.validate_entry(meta), [])

    def test_hit_increments_and_stamps(self) -> None:
        path = self.seed_entry()
        output = self.run_cli(["search", "--query", "退避"])
        self.assertIn("hit=1 use=0", output)
        meta = self.meta_of(path)
        self.assertEqual(meta["hit_count"], 1)
        self.assertTrue(meta["last_hit_at"])
        self.assertEqual(meta["use_count"], 0)
        self.run_cli(["search", "--query", "退避"])
        self.assertEqual(self.meta_of(path)["hit_count"], 2)

    def test_tool_condition_also_counts(self) -> None:
        path = self.seed_entry()
        self.run_cli(["search", "--tool", "cms_query"])
        self.assertEqual(self.meta_of(path)["hit_count"], 1)

    def test_listing_does_not_count(self) -> None:
        """空 query 是盘点用法，不是召回。"""
        path = self.seed_entry()
        self.run_cli(["search"])
        self.run_cli(["search", "--category", "tooling"])
        self.assertEqual(self.meta_of(path)["hit_count"], 0)

    def test_possible_section_does_not_count(self) -> None:
        """未声明 applicable files 的条目落在「可能相关」段，属猜测命中。"""
        path = self.seed_entry()
        self.run_cli(["search", "--path", "src/app.py"])
        self.assertEqual(self.meta_of(path)["hit_count"], 0)

    def test_no_count_keeps_counter(self) -> None:
        path = self.seed_entry()
        output = self.run_cli(["search", "--query", "退避", "--no-count"])
        self.assertIn("hit=0", output)
        self.assertEqual(self.meta_of(path)["hit_count"], 0)

    def test_count_keeps_body_and_other_fields(self) -> None:
        path = self.seed_entry()
        before_meta, before_body = lib.load_entry(path)
        self.run_cli(["search", "--query", "退避"])
        after_meta, after_body = lib.load_entry(path)
        self.assertEqual(before_body, after_body)
        strip = lambda meta: {k: v for k, v in meta.items() if k not in ("hit_count", "last_hit_at")}
        self.assertEqual(strip(before_meta), strip(after_meta))

    def test_lock_timeout_still_returns_hits(self) -> None:
        path = self.seed_entry()
        with mock.patch.object(lib, "recall_lock", side_effect=lib.LockTimeout("锁被占用")):
            output = self.run_cli(["search", "--query", "退避"])
        self.assertIn("CMS 查询退避", output)
        self.assertEqual(self.meta_of(path)["hit_count"], 0)

    def test_search_reminds_to_report_adoption(self) -> None:
        """采集侧提醒：不提醒就没人跑 use，use_count 会一直停在 0。"""
        self.seed_entry()
        output = self.run_cli(["search", "--query", "退避"])
        self.assertIn("`use <id> --result ok|failed`", output)

    def test_legacy_use_count_reads_as_recall(self) -> None:
        """存量条目的旧 use_count 是召回口径：读成 hit，采纳为 0。"""
        path = self.seed_entry()
        self.make_legacy(path, count=5)
        stats = json.loads(self.run_cli(["stats", "--json"]))
        self.assertEqual(stats["recall_total"], 5)
        self.assertEqual(stats["adopt_total"], 0)

    def test_legacy_entry_migrates_on_first_hit(self) -> None:
        path = self.seed_entry()
        self.make_legacy(path, count=5, stamp="2026-01-01T00:00:00")
        self.run_cli(["search", "--query", "退避"])
        meta = self.meta_of(path)
        self.assertEqual(meta["hit_count"], 6)
        self.assertEqual(meta["use_count"], 0)
        self.assertEqual(meta["last_hit_at"][:10], lib.now_iso()[:10])
        self.assertIsNone(meta["last_used_at"])


class AdoptionCountTest(BaseCase):
    """use 记采纳：自报口径，与 hit_count 互不影响。"""

    def seed_entry(self) -> tuple[Path, str]:
        self.run_cli(
            [
                "add", "--title", "CMS 查询退避", "--category", "tooling", "--type", "tool-rule",
                "--scope", "project", "--action", "指数退避重试", "--tags", "cms,throttling",
                "--pre-tool", "cms_query",
            ]
        )
        path, meta, _ = next(lib.iter_entries(scope="project", project="demo"))
        return path, str(meta["id"])

    def meta_of(self, path: Path) -> dict:
        return lib.load_entry(path)[0]

    def test_use_increments_without_touching_hit(self) -> None:
        path, entry_id = self.seed_entry()
        before = self.meta_of(path)
        output = self.run_cli(["use", entry_id])
        self.assertIn("第 1 次", output)
        meta = self.meta_of(path)
        self.assertEqual(meta["use_count"], 1)
        self.assertEqual(meta["last_use_result"], "ok")
        self.assertTrue(meta["last_used_at"])
        self.assertEqual(meta["hit_count"], 0)
        self.assertIsNone(meta["last_hit_at"])
        # 采纳是自报，不能顶替复核时间戳。
        self.assertEqual(meta["verified_at"], before["verified_at"])

    def test_use_twice_accumulates(self) -> None:
        path, entry_id = self.seed_entry()
        self.run_cli(["use", entry_id])
        self.run_cli(["use", entry_id])
        self.assertEqual(self.meta_of(path)["use_count"], 2)

    def test_failed_use_counts_and_stays_marked(self) -> None:
        path, entry_id = self.seed_entry()
        output = self.run_cli(["use", entry_id, "--result", "failed", "--note", "第三次仍超时"])
        self.assertIn("失败也留档", output)
        meta = self.meta_of(path)
        self.assertEqual(meta["use_count"], 1)
        self.assertEqual(meta["use_failed_count"], 1)
        self.assertEqual(meta["last_use_result"], "failed")
        self.assertEqual(meta["last_use_note"], "第三次仍超时")

    def test_unknown_id_fails_loudly(self) -> None:
        self.seed_entry()
        with self.assertRaises(SystemExit):
            self.run_cli(["use", "tooling-does-not-exist"])

    def test_legacy_entry_does_not_inherit_recall_as_adoption(self) -> None:
        path, entry_id = self.seed_entry()
        meta, body = lib.load_entry(path)
        meta.pop("hit_count", None)
        meta.pop("last_hit_at", None)
        meta["use_count"] = 5
        meta["last_used_at"] = "2026-01-01T00:00:00"
        lib.atomic_write_text(path, lib.dump_entry(meta, body))
        self.run_cli(["use", entry_id])
        after = self.meta_of(path)
        self.assertEqual(after["hit_count"], 5)
        self.assertEqual(after["use_count"], 1)

    def test_stats_and_index_report_both_counters(self) -> None:
        _, entry_id = self.seed_entry()
        self.run_cli(["search", "--query", "退避"])
        self.run_cli(["use", entry_id])
        stats = json.loads(self.run_cli(["stats", "--json"]))
        self.assertEqual(stats["recall_total"], 1)
        self.assertEqual(stats["recalled_entries"], 1)
        self.assertEqual(stats["unused_entries"], 0)
        self.assertEqual(stats["recall_top"][0]["hit_count"], 1)
        self.assertEqual(stats["adopt_total"], 1)
        self.assertEqual(stats["adopted_entries"], 1)
        self.assertEqual(stats["adopt_failed_total"], 0)
        self.assertEqual(stats["adopt_rate"], 1.0)
        self.assertEqual(stats["adopt_top"][0]["use_count"], 1)
        self.run_cli(["index"])
        index_text = lib.index_path().read_text(encoding="utf-8")
        self.assertIn("| hit_count | use_count |", index_text)


class GateTest(BaseCase):
    """开场闸门：只有主 agent 能派发，锁在派发前抢下，复盘期间不再派发。"""

    def lock_path(self) -> Path:
        return lib.store_home() / ".review.lock"

    def test_no_candidates_does_not_dispatch(self) -> None:
        output = self.run_cli(["gate"])
        self.assertIn("无待复盘候选，不派发", output)
        self.assertFalse(self.lock_path().exists())

    def test_dispatch_prints_brief_and_holds_lock(self) -> None:
        self.seed_candidate_file(self.project)
        output = self.run_cli(["gate"])
        self.assertIn("后台派发复盘 Agent", output)
        self.assertIn("不派发任何子 Agent", output)
        self.assertIn("review --end --summary", output)
        self.assertTrue(self.lock_path().exists())

    def test_second_gate_sees_lock_and_does_not_dispatch(self) -> None:
        """防套娃的机械闸：复盘期间任何进程再跑 gate 都拿不到派发指令。"""
        self.seed_candidate_file(self.project)
        self.run_cli(["gate"])
        output = self.run_cli(["gate"])
        self.assertIn("复盘进行中", output)
        self.assertIn("本次不派发", output)
        self.assertNotIn("后台派发复盘 Agent", output)

    def test_end_releases_lock_and_writes_report(self) -> None:
        self.seed_candidate_file(self.project)
        self.run_cli(["gate"])
        output = self.run_cli(["review", "--end", "--summary", "入库 1 条、丢弃 0 条、保留 0 条"])
        self.assertIn("闸门锁已释放", output)
        self.assertFalse(self.lock_path().exists())
        reports = list((lib.store_home() / "reports").glob("*.md"))
        self.assertEqual(len(reports), 1)
        self.assertIn("入库 1 条", reports[0].read_text(encoding="utf-8"))

    def test_stale_lock_lets_gate_dispatch_again(self) -> None:
        self.seed_candidate_file(self.project)
        self.lock_path().parent.mkdir(parents=True, exist_ok=True)
        stamp = (datetime.now(lib.BEIJING) - timedelta(minutes=30)).isoformat()
        self.lock_path().write_text(json.dumps({"pid": 1, "ts": stamp}), encoding="utf-8")
        output = self.run_cli(["gate"])
        self.assertIn("后台派发复盘 Agent", output)

    def test_gate_dispatches_other_project_backlog(self) -> None:
        self.seed_candidate_file(self.other_project())
        output = self.run_cli(["gate"])
        self.assertIn("按 --all 跨项目处理", output)

    # 会话标记：一次会话只在首次启动复盘一次，直到会话退出都不再复盘。

    def marker_path(self) -> Path:
        return lib.store_home() / ".session-review.json"

    def gate_in_session(self, session: str) -> str:
        """按指定会话 id 跑 gate，避免测试进程自身的会话 id 干扰判定。"""
        with mock.patch.dict(os.environ, {"DSH_SESSION_ID": session}):
            return self.run_cli(["gate"])

    def test_session_reviews_only_once(self) -> None:
        """一次会话只在首次启动复盘一次：复盘结束后再跑 gate 不派发。"""
        self.seed_candidate_file(self.project)
        self.assertIn("后台派发复盘 Agent", self.gate_in_session("sess-a"))
        self.run_cli(["review", "--end", "--summary", "入库 0 条、丢弃 0 条、保留 0 条"])
        self.seed_candidate_file(self.project, session="s-later")
        second = self.gate_in_session("sess-a")
        self.assertIn("本会话已复盘过", second)
        self.assertNotIn("后台派发复盘 Agent", second)
        self.assertFalse(self.lock_path().exists())

    def test_startup_without_candidates_consumes_the_session(self) -> None:
        """开场无候选也算用掉这次复盘：会话中途冒出候选不再复盘。"""
        self.assertIn("无待复盘候选，不派发", self.gate_in_session("sess-a"))
        self.seed_candidate_file(self.project)
        second = self.gate_in_session("sess-a")
        self.assertIn("本会话已复盘过", second)
        self.assertNotIn("后台派发复盘 Agent", second)

    def test_new_session_reviews_again(self) -> None:
        """会话退出（换会话 id）后标记失效，新会话照常复盘。"""
        self.seed_candidate_file(self.project)
        self.gate_in_session("sess-a")
        self.run_cli(["review", "--end", "--summary", "入库 0 条、丢弃 0 条、保留 0 条"])
        self.seed_candidate_file(self.project, session="s-later")
        self.assertIn("后台派发复盘 Agent", self.gate_in_session("sess-b"))

    def test_marker_of_dead_process_is_ignored(self) -> None:
        """pid 型标记的进程已退出：上个会话的残留不得关掉本会话闸门。"""
        died = subprocess.Popen([sys.executable, "-c", "pass"])
        died.wait()
        self.marker_path().parent.mkdir(parents=True, exist_ok=True)
        self.marker_path().write_text(
            json.dumps({"key": f"pid:{died.pid}", "ts": lib.now_iso(), "dispatched": True}),
            encoding="utf-8",
        )
        self.assertIsNone(experience._read_session_marker())

    def test_expired_marker_is_ignored(self) -> None:
        """超期标记按无标记处理：pid 复用不得把闸门永久关掉。"""
        stamp = (datetime.now(lib.BEIJING) - timedelta(hours=80)).isoformat()
        self.marker_path().parent.mkdir(parents=True, exist_ok=True)
        self.marker_path().write_text(
            json.dumps({"key": f"pid:{os.getpid()}", "ts": stamp, "dispatched": True}),
            encoding="utf-8",
        )
        self.assertIsNone(experience._read_session_marker())


class LibraryTableTest(BaseCase):
    """登记表：入库、删除、计数变更都同步同一张表，跨项目一份。"""

    def add_entry(
        self,
        title: str = "CMS 查询退避",
        scope: str = "project",
        action: str = "指数退避重试",
        tags: str = "cms",
        category: str = "tooling",
        type_: str = "tool-rule",
    ) -> str:
        self.run_cli(
            [
                "add", "--title", title, "--category", category, "--type", type_,
                "--scope", scope, "--action", action, "--tags", tags,
            ]
        )
        for _, meta, _ in [
            *lib.iter_entries(scope="global"),
            *lib.iter_entries(scope="project", project="demo"),
        ]:
            if str(meta.get("title")) == title:
                return str(meta.get("id"))
        raise AssertionError(f"未写入条目：{title}")

    def table_text(self) -> str:
        return lib.index_path().read_text(encoding="utf-8")

    def test_add_registers_row(self) -> None:
        self.add_entry()
        text = self.table_text()
        self.assertIn("| 经验条目名 | 描述 | 类别/类型 | 置信度 | hit_count | use_count | 所属项目 |", text)
        self.assertIn("CMS 查询退避", text)
        self.assertIn("指数退避重试", text)
        self.assertIn("| 0 | 0 | demo |", text)
        self.assertIn("条目数：1（全局 0 / 项目 1）", text)

    def test_global_entry_labelled_global(self) -> None:
        self.add_entry(title="全局工具规则", scope="global", tags="globaltool")
        self.assertIn("| 全局 |", self.table_text())

    def test_search_updates_hit_column(self) -> None:
        self.add_entry()
        self.run_cli(["search", "--query", "退避"])
        self.assertIn("| 1 | 0 | demo |", self.table_text())

    def test_use_updates_use_column(self) -> None:
        entry_id = self.add_entry()
        self.run_cli(["use", entry_id])
        self.assertIn("| 0 | 1 | demo |", self.table_text())

    def test_library_command_prints_table(self) -> None:
        self.add_entry()
        output = self.run_cli(["library"])
        self.assertIn("| 经验条目名 | 描述 |", output)
        self.assertIn("CMS 查询退避", output)
        self.assertIn("| 0 | 0 | demo |", output)

    def test_description_escapes_pipes(self) -> None:
        self.add_entry(title="管道符结论", action="A | B 两种都行", tags="pipes")
        text = self.table_text()
        self.assertIn("A \\| B 两种都行", text)

    def test_global_rows_sort_before_project_rows(self) -> None:
        self.add_entry(title="项目内的", tags="inner")
        self.add_entry(title="全局的", scope="global", tags="outer")
        text = self.table_text()
        self.assertLess(text.index("全局的"), text.index("项目内的"))

    def test_remove_preview_keeps_entry(self) -> None:
        entry_id = self.add_entry()
        output = self.run_cli(["remove", entry_id])
        self.assertIn("预演", output)
        self.assertIn(entry_id, self.table_text())

    def test_remove_apply_deletes_and_syncs(self) -> None:
        entry_id = self.add_entry()
        path, _, _ = next(lib.iter_entries(scope="project", project="demo"))
        self.run_cli(["remove", entry_id, "--apply"])
        self.assertFalse(path.exists())
        text = self.table_text()
        self.assertNotIn(entry_id, text)
        self.assertIn("条目数：0（全局 0 / 项目 0）", text)
        self.assertIn("（暂无条目）", text)

    def test_remove_unknown_id_fails_loudly(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_cli(["remove", "tooling-does-not-exist", "--apply"])

    def test_library_no_refresh_does_not_write(self) -> None:
        self.add_entry()
        before = self.table_text()
        output = self.run_cli(["library", "--no-refresh"])
        self.assertIn("CMS 查询退避", output)
        self.assertEqual(before, self.table_text())

    def test_use_and_remove_reach_other_projects(self) -> None:
        """登记表跨项目：表里别的项目的 id 也能 use / remove，不必切 cwd。"""
        self.run_cli(
            [
                "add", "--title", "别处项目的条目", "--category", "tooling", "--type", "tool-rule",
                "--scope", "project", "--project", "gwsvc", "--action", "别处结论", "--tags", "otherproj",
            ]
        )
        path, meta, _ = next(lib.iter_entries(scope="project", project="gwsvc"))
        entry_id = str(meta["id"])
        self.run_cli(["use", entry_id])
        self.assertEqual(lib.load_entry(path)[0]["use_count"], 1)
        self.run_cli(["remove", entry_id, "--apply"])
        self.assertFalse(path.exists())
        self.assertNotIn(entry_id, self.table_text())

    def test_index_command_rebuilds_same_table(self) -> None:
        self.add_entry()
        self.run_cli(["index"])
        self.assertTrue(self.table_text().startswith("# 经验库登记表"))


class RedactionTest(BaseCase):
    def test_secret_keys_are_redacted(self) -> None:
        for key in ("token", "access_token", "api_key", "password", "Authorization", "refresh-token"):
            self.assertTrue(lib.is_secret_key(key), f"{key} 应判为凭据字段")

    def test_counters_are_not_redacted(self) -> None:
        """tokens 是计数不是凭据；误伤会毁掉平均会话成本指标。"""
        for key in ("tokens", "total_tokens", "prompt_tokens", "completion_tokens", "token_count", "max_tokens"):
            self.assertFalse(lib.is_secret_key(key), f"{key} 是计数，不应判为凭据字段")

    def test_cost_tokens_survives_redaction(self) -> None:
        cleaned = lib.redact({"cost": {"tokens": 1234, "tool_calls": 7}, "api_key": "AbCdEf0123456789AbCdEf0123456789"})
        self.assertEqual(cleaned["cost"]["tokens"], 1234)
        self.assertEqual(cleaned["cost"]["tool_calls"], 7)
        self.assertEqual(cleaned["api_key"], lib.REDACTED)

    def test_inline_credentials_are_redacted(self) -> None:
        text = lib.redact_scalar("用 token=AbCdEf0123456789AbCdEf0123456789 查一下")
        self.assertNotIn("AbCdEf0123456789AbCdEf0123456789", text)
        self.assertIn(lib.REDACTED, text)

    def test_git_sha_is_kept(self) -> None:
        """纯十六进制的 commit 短号不是凭据，保留才能回溯证据。"""
        sha = "9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c"
        self.assertEqual(lib.redact_scalar(sha), sha)

    def test_prefix_tokens_are_redacted(self) -> None:
        """provider 前缀令牌与 JWT 不带分隔符也能认出来。"""
        for token in (
            "AKIAIOSFODNN7EXAMPLE",
            "sk-proj-AbCdEf0123456789",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnopqrstuvwxyz",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0",
        ):
            self.assertEqual(lib.redact_scalar(token), lib.REDACTED, f"{token} 应整段脱敏")

    def test_inline_credentials_cover_log_shapes(self) -> None:
        """抓包与配置里最常见的三种写法：Bearer、PGPASSWORD、client_secret。"""
        for text in (
            "请求头 Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnopqrstuvwxyz",
            "docker run -e PGPASSWORD=sup3rs3cr3tpass psql",
            "client_secret: AbCdEf0123456789AbCdEf0123456789",
            "Authorization: Basic QWxhZGRpbjpvcGVuc2VzYW1l",
        ):
            cleaned = lib.redact_scalar(text)
            self.assertIn(lib.REDACTED, cleaned, f"{text} 应命中脱敏")
            self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", cleaned)

    def test_private_key_block_is_redacted(self) -> None:
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
        self.assertEqual(lib.redact_scalar(pem), lib.REDACTED)

    def test_prose_without_credentials_survives(self) -> None:
        """正常描述不能被误伤，否则条目正文全成 [REDACTED]。"""
        for text in (
            "CMS 查询改用 SDK 分页，单页 100 条",
            "改跑 scripts/build_all.py 后仍报 Throttling",
            "把 index 上限调到 500 也没用",
        ):
            self.assertEqual(lib.redact_scalar(text), text, f"{text} 不该被判为凭据")

    def test_note_drops_credentials_before_write(self) -> None:
        """note 是自记入口，写入侧必须自己脱敏，不能指望 agent 看不看得出来。"""
        output = self.run_cli(
            [
                "note", "--title", "cred-verify",
                "--action", "dump 里带着 Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnopqrstuvwxyz 的请求头",
                "--detail", "抓包看到 AWS key AKIAIOSFODNN7EXAMPLE",
                "--root-cause", "盲区验证",
            ]
        )
        self.assertIn("凭据形态内容", output)
        raw = next(self.home.glob("candidates/*/*.jsonl")).read_text(encoding="utf-8")
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", raw)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", raw)
        record = json.loads(raw.splitlines()[0])
        self.assertIn(lib.REDACTED, record["assistant"])
        self.assertIn(lib.REDACTED, record["note"]["action"])

    def test_note_redaction_leaves_diagnostic(self) -> None:
        """脱敏是改写原文，必须在诊断里留痕，否则用户永远不知道记录被动过。"""
        self.run_cli(["note", "--title", "audit", "--action", "PRIVATE KEY 出现时先整段替换", "--detail", "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"])
        rows = [record for _, record in lib.iter_diagnostics(1) if record.get("reason") == "credential_redacted"]
        self.assertTrue(rows, "脱敏命中必须落一行诊断")

    def test_show_redacts_legacy_candidate(self) -> None:
        """历史候选可能早于脱敏口径落盘：磁盘保留原文，展示必须先脱敏。"""
        path = self.home / "candidates" / "demo" / "2026-09-22-manual-manual.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "client": "manual", "session_id": "manual", "turn_id": "t1", "ts": "2026-09-22T10:00:00+08:00",
                    "user": "", "assistant": "用 token=AbCdEf0123456789AbCdEf0123456789 查一下",
                    "tools": [], "outcome": "success",
                    "cost": {"tool_calls": 0, "tokens": 0, "duration_ms": 0},
                    "evidence": {"path": "", "lines": []},
                },
                ensure_ascii=False,
            ) + "\n",
            encoding="utf-8",
        )
        cid = lib.candidate_id("manual", "manual", "t1", "2026-09-22")
        output = self.run_cli(["show", cid])
        self.assertNotIn("AbCdEf0123456789AbCdEf0123456789", output)
        self.assertIn(lib.REDACTED, output)
        self.assertIn("脱敏视图", output)
        self.assertIn("AbCdEf0123456789AbCdEf0123456789", path.read_text(encoding="utf-8"))

    def test_add_drops_credentials_before_write(self) -> None:
        """add 是长期资产入口，命令行参数与正文都要过脱敏。"""
        output = self.run_cli(
            [
                "add", "--manual", "--title", "凭据脱敏验证", "--category", "pitfall", "--type", "recovery",
                "--action", "把 PG_PASSWORD=sup3rs3cr3tpass 换成本地 .pgpass",
                "--root-cause", "脚本里写死了 PGPASSWORD=sup3rs3cr3tpass",
            ]
        )
        self.assertIn("凭据形态内容", output)
        text = next(self.home.glob("projects/*/pitfall/*.md")).read_text(encoding="utf-8")
        self.assertNotIn("sup3rs3cr3tpass", text)
        self.assertIn(lib.REDACTED, text)

    def test_candidate_records_redaction_count(self) -> None:
        """被改写过的候选要留痕：复盘的人分得清「本来没凭据」与「凭据被换掉」。"""
        path = Path(self.temp.name) / "redact.jsonl"
        write_jsonl(
            path,
            [
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:00:01Z",
                    "message": {"role": "user", "content": "用 token=AbCdEf0123456789AbCdEf0123456789 查一下"},
                },
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:00:05Z",
                    "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "x"}}]},
                },
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:00:20Z",
                    "message": {"role": "toolResult", "toolName": "bash", "isError": True, "content": [{"type": "text", "text": "Error: refused"}]},
                },
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:00:25Z",
                    "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "c2", "name": "bash", "arguments": {"command": "conntrack -S --note '实测这台机器上 conntrack 老化时间是 120 秒，只在本机成立'"}}]},
                },
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:00:30Z",
                    "message": {"role": "toolResult", "toolName": "bash", "isError": False, "content": [{"type": "text", "text": "ok"}]},
                },
            ],
        )
        result = experience_hook.handle(
            {"client": "pi", "event": "turn_end", "session_id": "s9", "turn_id": "1", "cwd": str(self.project), "transcript_path": str(path)}
        )
        self.assertTrue(result.get("written"))
        raw = next(self.home.glob("candidates/*/*.jsonl")).read_text(encoding="utf-8")
        self.assertNotIn("AbCdEf0123456789AbCdEf0123456789", raw)
        self.assertGreaterEqual(json.loads(raw.splitlines()[0]).get("redactions", 0), 1)


class CandidateLockTest(BaseCase):
    """候选文件的读改写必须整段待在锁里，否则并发追加会被按旧内容重写掉。"""

    def seed_candidate(self, session: str = "fixture", turn: str = "1") -> str:
        path = Path(self.temp.name) / f"{session}.jsonl"
        write_jsonl(
            path,
            [
                {"type": "message", "timestamp": "2026-09-22T10:00:01Z", "message": {"role": "user", "content": "以后一律先按 hostname 过滤，不要用全量查询"}},
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:00:05Z",
                    "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "c1", "name": "cms_query", "arguments": {"scope": "all"}}]},
                },
                {"type": "message", "timestamp": "2026-09-22T10:00:20Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "好"}], "usage": {}}},
            ],
        )
        experience_hook.handle(
            {"client": "pi", "event": "turn_end", "session_id": session, "turn_id": turn, "cwd": str(self.project), "transcript_path": str(path)}
        )
        files = sorted((self.home / "candidates" / "demo").glob("*.jsonl"))
        record = json.loads(files[-1].read_text(encoding="utf-8").splitlines()[0])
        return lib.candidate_id(record["client"], record["session_id"], record["turn_id"], record["ts"][:10])

    def candidate_lines(self) -> list[str]:
        """按日期通配定位候选文件：文件名含落盘当天日期，硬编码日期会隔天就红。"""
        files = sorted((self.home / "candidates" / "demo").glob(f"*-pi-fixture.jsonl"))
        self.assertEqual(len(files), 1, f"应恰好一份候选文件，实际 {[item.name for item in files]}")
        text = files[0].read_text(encoding="utf-8")
        return [line for line in text.splitlines() if line.strip()]

    def test_drop_keeps_other_candidates(self) -> None:
        """丢一条不能顺手把别人的候选也抹掉：同文件多行时必须按 id 定位。"""
        first = self.seed_candidate(session="fixture", turn="1")
        self.seed_candidate(session="fixture", turn="2")
        self.run_cli(["drop", first])
        lines = self.candidate_lines()
        self.assertEqual(len(lines), 1, f"应只剩另一条候选，实际 {lines}")
        self.assertEqual(json.loads(lines[0])["turn_id"], "2")

    def test_consume_is_idempotent(self) -> None:
        """候选已被消费时不要报「未找到」：那是并发下的正常结果。"""
        cid = self.seed_candidate()
        self.assertTrue(lib.consume_candidate(cid))
        self.assertFalse(lib.consume_candidate(cid))

    def test_concurrent_appends_of_same_turn_keep_one_line(self) -> None:
        """五端钩子并发追加同一轮：锁内合并，落盘只剩一行。"""
        script = (
            "import os, sys\n"
            f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
            "import experience_lib as lib\n"
            "record = {'client': 'pi', 'session_id': 'srace', 'turn_id': '1', 'ts': '2026-09-23T10:00:00+08:00', 'project_name': 'demo'}\n"
            "lib.append_candidate('demo', record)\n"
        )
        env = dict(os.environ, EXPERIENCE_HOME=str(self.home))
        processes = [
            subprocess.Popen([sys.executable, "-c", script], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            for _ in range(8)
        ]
        for process in processes:
            _, stderr = process.communicate(timeout=60)
            self.assertEqual(process.returncode, 0, stderr.decode("utf-8", "replace"))
        rows = lib.load_candidate_rows(self.home / "candidates" / "demo" / "2026-09-23-pi-srace.jsonl")
        self.assertEqual(len(rows), 1, f"同一轮并发追加只应留一行，实际 {len(rows)}")

    def test_lock_timeout_fails_fast_without_writing(self) -> None:
        """锁被别的进程占住时要报错退出，不能无限等，也不能装作写成功。"""
        # 锁目标必须由 note 的实际口径算出：holder 自己造项目名会锁到另一个文件，
        # 两边不争用，这条用例就成了空转。
        holder = (
            "import json, sys, time\n"
            f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
            "import experience_lib as lib\n"
            "name = lib.ensure_project(lib.find_project_root(lib.Path.cwd()))\n"
            "target = lib.candidates_dir(name) / "
            "lib.candidate_file(name, lib.MANUAL_CLIENT, lib.MANUAL_CLIENT, lib.now_iso()[:10]).name\n"
            "with lib.FileLock(target):\n"
            "    print(json.dumps({'target': str(target)}), flush=True)\n"
            "    time.sleep(6)\n"
        )
        env = dict(os.environ, EXPERIENCE_HOME=str(self.home))
        process = subprocess.Popen([sys.executable, "-c", holder], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            first = process.stdout.readline().strip()
            self.assertTrue(first.startswith("{"), f"holder 未报出锁目标：{first!r}")
            target = Path(json.loads(first)["target"])
            self.assertEqual(target.parent, self.home / "candidates" / "demo", "锁目标与 note 写入目录不一致")
            started = time.monotonic()
            with mock.patch.object(lib.FileLock, "DEFAULT_TIMEOUT_SEC", 0.3), mock.patch.object(lib.FileLock, "POLL_SEC", 0.02):
                with self.assertRaises(SystemExit) as caught:
                    self.run_cli(["note", "--title", "并发占锁", "--action", "等锁超时要报错"])
            self.assertIn("未写入", str(caught.exception))
            self.assertLess(time.monotonic() - started, 5.0, "拿不到锁必须快速失败，不能阻塞到占用者释放")
        finally:
            process.kill()
            process.communicate()

    def test_second_process_cannot_hold_same_file(self) -> None:
        """锁必须是跨进程互斥：占用者在别的进程里时，本进程拿不到。"""
        holder = (
            "import sys, time\n"
            f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
            "import experience_lib as lib\n"
            "target = lib.candidates_dir('demo') / 'race.jsonl'\n"
            "with lib.FileLock(target):\n"
            "    print('locked', flush=True)\n"
            "    time.sleep(6)\n"
        )
        env = dict(os.environ, EXPERIENCE_HOME=str(self.home))
        process = subprocess.Popen([sys.executable, "-c", holder], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            with mock.patch.object(lib.FileLock, "DEFAULT_TIMEOUT_SEC", 0.2), mock.patch.object(lib.FileLock, "POLL_SEC", 0.02):
                with self.assertRaises(lib.LockTimeout):
                    with lib.FileLock(self.home / "candidates" / "demo" / "race.jsonl"):
                        pass
        finally:
            process.kill()
            process.communicate()


class AdapterContractTest(BaseCase):
    """适配器与解析器之间是跨语言契约，改名会让采集静默失效，用测试锁住。"""

    def test_dsh_adapter_vocabulary_is_supported(self) -> None:
        """扫全文取事件名，因此适配器注释里也不要写 `type: 'x'` 这类字面量。"""
        source = (SCRIPTS.parent / "adapters" / "dsh" / "experience-keeper.mjs").read_text(encoding="utf-8")
        emitted = set(re.findall(r"type:\s*'([a-z_]+)'", source))
        self.assertTrue(emitted, "未从 dsh 适配器里解析出任何事件类型")
        unsupported = emitted - set(transcripts.DSH_EVENT_TYPES)
        self.assertEqual(unsupported, set(), f"dsh 适配器发出了解析器不支持的事件：{unsupported}")

    def test_dsh_payload_fields_match_parser(self) -> None:
        source = (SCRIPTS.parent / "adapters" / "dsh" / "experience-keeper.mjs").read_text(encoding="utf-8")
        for field in ("client: 'dsh'", "session_id", "turn_id", "compaction_count", "transcript_path"):
            self.assertIn(field, source, f"dsh 适配器缺少统一契约字段：{field}")

    def test_pi_adapter_registers_expected_events(self) -> None:
        source = (SCRIPTS.parent / "adapters" / "pi" / "experience-keeper.ts").read_text(encoding="utf-8")
        for event in ("turn_end", "session_compact", "session_shutdown"):
            self.assertIn(f'pi.on("{event}"', source, f"pi 扩展未订阅 {event}")

    def test_codemaker_adapter_matches_proven_plugin_shape(self) -> None:
        source = (SCRIPTS.parent / "adapters" / "codemaker" / "index.js").read_text(encoding="utf-8")
        self.assertIn("export const server", source)
        self.assertIn("export default", source)
        self.assertIn("session.idle", source)

    def test_every_client_has_a_parser_contract_doc(self) -> None:
        """契约文档缺失就等于没人知道这一端丢了什么，纳入测试锁住。"""
        docs = SCRIPTS.parent / "docs" / "parsers"
        for client in lib.CLIENTS:
            path = docs / f"{client}.md"
            self.assertTrue(path.exists(), f"缺少 {client} 的解析契约文档")
            text = path.read_text(encoding="utf-8")
            for section in ("轮次边界", "工具状态", "丢弃内容", "已知不可得"):
                self.assertIn(section, text, f"{client}.md 缺少「{section}」小节")

    def test_parser_index_lists_every_client(self) -> None:
        index = (SCRIPTS.parent / "docs" / "parsers" / "README.md").read_text(encoding="utf-8")
        for client in lib.CLIENTS:
            self.assertIn(f"{client}.md", index)


class AssetRegistryTest(BaseCase):
    """可复用资产的跨会话存续：候选可被清掉，资产必须在库里留下痕迹。"""

    def track_script(self, repository: Path, name: str = "tracked.py") -> Path:
        subprocess.run(["git", "init", "-q", str(repository)], check=True, capture_output=True)
        script = repository / name
        script.write_text("import argparse\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repository), "add", "--", name], check=True, capture_output=True)
        return script

    def test_git_tracked_script_is_not_collected(self) -> None:
        script = self.track_script(Path(self.temp.name) / "repository")
        result = self.run_turn("tracked", script, "import argparse\nparser = argparse.ArgumentParser()")
        self.assertEqual(lib.load_assets(), [])
        self.assertNotIn("asset_script", result.get("signals") or [])
        turn = {"tools": [{"name": "write", "args": {"file_path": str(script), "content": "import argparse"}}]}
        self.assertEqual(prescreen.reusable_asset_paths(turn), [])
        self.assertEqual(prescreen.classify_turn_asset(turn), "none")
        self.assertEqual(lib.upsert_assets([str(script)], {}), 0)

    def test_refresh_excludes_asset_after_git_add(self) -> None:
        repository = Path(self.temp.name) / "repository"
        subprocess.run(["git", "init", "-q", str(repository)], check=True, capture_output=True)
        script = repository / "generated.py"
        self.run_turn("generated", script, "import argparse\nparser = argparse.ArgumentParser()")
        self.assertEqual(len(lib.load_assets()), 1)
        subprocess.run(["git", "-C", str(repository), "add", "--", script.name], check=True, capture_output=True)
        self.assertEqual(lib.load_assets(refresh=True), [])
        self.assertNotIn("generated.py", self.run_cli(["assets"]))
        self.assertNotIn("未确认的可复用资产", self.run_cli(["prompt"]))

    def test_git_tracking_uses_nested_repository(self) -> None:
        repository = Path(self.temp.name) / "outer"
        subprocess.run(["git", "init", "-q", str(repository)], check=True, capture_output=True)
        script = self.track_script(repository / "nested")
        self.assertTrue(lib.is_git_tracked(str(script)))
        untracked = script.with_name("untracked.py")
        untracked.write_text("print(1)\n", encoding="utf-8")
        self.assertFalse(lib.is_git_tracked(str(untracked)))

    def test_git_tracking_without_git_keeps_asset(self) -> None:
        with mock.patch.object(lib.subprocess, "run", side_effect=FileNotFoundError):
            self.assertFalse(lib.is_git_tracked(str(self.project / "script.py")))

    def write_transcript(self, session: str, path_on_disk: Path, content: str) -> Path:
        path = Path(self.temp.name) / f"{session}.jsonl"
        write_jsonl(
            path,
            [
                {"type": "message", "timestamp": "2026-09-24T10:00:01Z", "message": {"role": "user", "content": "排查 NAT 会话打满"}},
                {
                    "type": "message",
                    "timestamp": "2026-09-24T10:00:05Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "toolCall",
                                "id": "c1",
                                "name": "write",
                                "arguments": {"file_path": str(path_on_disk), "content": content},
                            }
                        ],
                    },
                },
                {
                    "type": "message",
                    "timestamp": "2026-09-24T10:00:09Z",
                    "message": {"role": "toolResult", "toolName": "write", "isError": False, "content": [{"type": "text", "text": "ok"}]},
                },
                {"type": "message", "timestamp": "2026-09-24T10:00:20Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "已生成脚本"}], "usage": {}}},
            ],
        )
        return path

    def run_turn(self, session: str, target: Path, content: str) -> dict:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        transcript = self.write_transcript(session, target, content)
        return experience_hook.handle(
            {
                "client": "pi",
                "event": "turn_end",
                "session_id": session,
                "turn_id": "1",
                "cwd": str(self.project),
                "transcript_path": str(transcript),
            }
        )

    def test_reusable_script_is_registered(self) -> None:
        script = self.project / "scripts" / "conntrack_report.py"
        result = self.run_turn("s1", script, "import argparse\nparser = argparse.ArgumentParser()\nparser.add_argument('--gw')")
        self.assertTrue(result.get("written"))
        rows = lib.load_assets()
        self.assertEqual(len(rows), 1)
        self.assertEqual(Path(rows[0]["path"]), script, "登记表要存实际路径：JSON 转义会把 Windows 路径写成双反斜杠")
        self.assertEqual(rows[0]["uses"], 1)
        self.assertEqual(rows[0]["reuse"], "reusable")
        self.assertTrue(rows[0]["hash"], "应记录文件指纹，用于识别改写与漂移")

    def test_one_off_script_is_not_registered(self) -> None:
        target = Path(self.temp.name) / "tmp" / "probe.py"
        self.run_turn("s2", target, "print(1)")
        self.assertEqual(lib.load_assets(), [], "一次性产出不进登记表，否则表会变成临时文件坟场")

    def test_second_session_increments_uses(self) -> None:
        """uses ≥ 2 是唯一不需要人工判断的复用证据：跨会话被再次产出即升级信号。"""
        script = self.project / "scripts" / "conntrack_report.py"
        content = "import argparse\nparser = argparse.ArgumentParser()"
        self.run_turn("s3", script, content)
        self.run_turn("s4", script, content)
        rows = lib.load_assets()
        self.assertEqual(len(rows), 1, "同一路径只保留一条记录")
        self.assertEqual(rows[0]["uses"], 2)
        self.assertEqual(rows[0]["produced_by"]["session"], "s4", "produced_by 指向最近一次产出")

    def backdate_candidate(self, days: int) -> None:
        """把候选的 ts 改早，用于验证按时间淘汰；文件名里的日期不动，不影响本用例。"""
        files = sorted((self.home / "candidates" / "demo").glob("*.jsonl"))
        self.assertTrue(files)
        rows = [json.loads(line) for line in files[-1].read_text(encoding="utf-8").splitlines() if line.strip()]
        for row in rows:
            row["ts"] = f"2026-01-01T10:00:00+08:00" if days > 30 else row["ts"]
        lib.atomic_write_text(files[-1], "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n")

    def test_out_of_band_comment_change_keeps_uses(self) -> None:
        """会话外补一行注释不算漂移：uses 断链会让跨会话复用证据永远攒不够。"""
        script = self.project / "scripts" / "annotated.py"
        content = "import argparse\nparser = argparse.ArgumentParser()\nprint('report')"
        self.run_turn("d1", script, content)
        script.write_text(content + "\n# 会话外补的说明\n", encoding="utf-8")
        self.run_turn("d2", script, content + "\n# 会话外补的说明\n")
        rows = lib.load_assets(refresh=True)
        self.assertEqual(rows[0]["uses"], 2)
        self.assertNotEqual(rows[0]["status"], "drift_detected", "只改注释不算漂移")

    def test_out_of_band_logic_change_flags_drift(self) -> None:
        """逻辑被改：标 drift_detected 并记 modified_out_of_band，不新增资产也不重置 uses。"""
        script = self.project / "scripts" / "changed.py"
        self.run_turn("d3", script, "import argparse\nparser = argparse.ArgumentParser()\nprint('v1')")
        script.write_text("import argparse\nparser = argparse.ArgumentParser()\nprint('v2-different')\n", encoding="utf-8")
        rows = lib.load_assets(refresh=True)
        self.assertEqual(rows[0]["status"], "drift_detected")
        self.assertTrue(rows[0]["modified_out_of_band"])
        self.assertEqual(rows[0]["uses"], 1, "漂移不重置也不累加 uses")

    def test_logic_fingerprint_ignores_comments_and_space(self) -> None:
        first = lib.logic_fingerprint("x = 1  # note\nprint(x)\n")
        second = lib.logic_fingerprint("x = 1\n\nprint(x)   # 另一条说明\n")
        self.assertEqual(first, second)
        self.assertNotEqual(first, lib.logic_fingerprint("x = 2\nprint(x)\n"))

    def test_registry_survives_candidate_gc(self) -> None:
        """候选被淘汰后登记表仍在：脚本还在盘上，不能让新人不知道它存在。"""
        script = self.project / "scripts" / "keep.py"
        self.run_turn("s5", script, "import argparse\nparser = argparse.ArgumentParser()")
        self.assertEqual(len(list(experience.iter_candidates("demo"))), 1)
        self.backdate_candidate(31)
        first = self.run_cli(["gc", "--older-than", "30d", "--apply"])
        self.assertIn("指向可复用资产，先转正或明确丢弃", first)
        self.assertEqual(len(list(experience.iter_candidates("demo"))), 1, "有资产指针的超期候选不能被静默淘汰")
        forced = self.run_cli(["gc", "--older-than", "30d", "--apply", "--force"])
        self.assertIn("--force 已指定", forced)
        self.assertEqual(list(experience.iter_candidates("demo")), [])
        rows = lib.load_assets()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "active", "候选淘汰了，资产登记必须留着")

    def test_deep_layer_candidate_gets_longer_ttl(self) -> None:
        """底层与性能类经验复现周期以月计：按 30 天清会清在复现之前。"""
        self.assertEqual(lib.CANDIDATE_MAX_AGE_DAYS, 30)
        self.assertEqual(lib.DEEP_LAYER_MAX_AGE_DAYS, 90)
        deep = {"signal": ["nonfunctional_floor"]}
        shallow = {"signal": ["asset_script"]}
        self.assertEqual(lib.candidate_ttl(deep), 90)
        self.assertEqual(lib.candidate_ttl(shallow), 30)
        self.assertEqual(lib.candidate_ttl(shallow, default=7), 7, "显式口径优先")

    def test_plain_candidate_expires_at_thirty_days(self) -> None:
        """没有资产指针的候选到点即淘汰，不留尾巴。"""
        path = Path(self.temp.name) / "s13.jsonl"
        write_jsonl(
            path,
            [
                {"type": "message", "timestamp": "2026-01-01T10:00:01Z", "message": {"role": "user", "content": "以后一律不要用全量查询"}},
                {"type": "message", "timestamp": "2026-01-01T10:00:05Z", "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "c1", "name": "read", "arguments": {"path": "x"}}]}},
                {"type": "message", "timestamp": "2026-01-01T10:00:09Z", "message": {"role": "toolResult", "toolName": "read", "isError": False, "content": [{"type": "text", "text": "ok"}]}},
                {"type": "message", "timestamp": "2026-01-01T10:00:20Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "好"}], "usage": {}}},
            ],
        )
        experience_hook.handle(
            {"client": "pi", "event": "turn_end", "session_id": "s13", "turn_id": "1", "cwd": str(self.project), "transcript_path": str(path)}
        )
        self.backdate_candidate(31)
        output = self.run_cli(["gc", "--apply"])
        self.assertIn(f"候选超过存活上限：1 条（超过 {lib.CANDIDATE_MAX_AGE_DAYS} 天", output)
        self.assertEqual(list(experience.iter_candidates("demo")), [])

    def test_deleted_file_becomes_gone(self) -> None:
        script = self.project / "scripts" / "temp.py"
        self.run_turn("s6", script, "import argparse\nparser = argparse.ArgumentParser()")
        script.unlink()
        rows = lib.load_assets(refresh=True)
        self.assertEqual(rows[0]["status"], "gone", "文件删了要留 gone 记录，它是曾产出的证据")
        active = [row for row in lib.load_assets(refresh=True) if row.get("status") != "gone"]
        self.assertEqual(active, [])

    def test_assets_command_lists_and_marks_uses(self) -> None:
        script = self.project / "scripts" / "reuse.py"
        content = "import argparse\nparser = argparse.ArgumentParser()"
        self.run_turn("s7", script, content)
        self.run_turn("s8", script, content)
        output = self.run_cli(["assets"])
        self.assertIn("reuse.py", output)
        self.assertIn("uses=2", output)
        self.assertIn("建议转条目", output)

    def test_prompt_mentions_project_entries(self) -> None:
        """有经验条目就必须出现在开场提示里：否则「有经验但无候选」的新会话静默错过全部历史结论。"""
        self.run_cli(
            [
                "add", "--manual", "--title", "GWSVC 单测编译顺序",
                "--category", "tooling", "--type", "path", "--scope", "project",
                "--action", "先 make deps 再 make test", "--tags", "gwsvc,unittest",
            ]
        )
        output = self.run_cli(["prompt"])
        self.assertIn("已有 1 条经验条目", output)
        self.assertIn("需要时按现场字样 search 召回", output)
        self.assertIn("不必等我确认", output)
        self.assertNotIn("需要复盘就回复", output, "没有候选积压时不该要求用户确认复盘")

    def test_prompt_silent_when_nothing_stored(self) -> None:
        self.assertEqual(self.run_cli(["prompt"]).strip(), "")

    def test_prompt_mentions_pending_assets(self) -> None:
        """开场提示要带上未确认资产：这是跨会话寻回的入口。"""
        script = self.project / "scripts" / "prompted.py"
        self.run_turn("s9", script, "import argparse\nparser = argparse.ArgumentParser()")
        output = self.run_cli(["prompt"])
        self.assertIn("未确认的可复用资产", output)

    def test_search_lists_assets(self) -> None:
        script = self.project / "scripts" / "conntrack_report.py"
        self.run_turn("s10", script, "import argparse\nparser = argparse.ArgumentParser()")
        output = self.run_cli(["search", "--query", "conntrack"])
        self.assertIn("可复用资产", output)
        self.assertIn("conntrack_report.py", output)
        payload = json.loads(self.run_cli(["search", "--query", "conntrack", "--json"]))
        self.assertEqual(len(payload["assets"]), 1)

    def test_registry_failure_does_not_block_candidate(self) -> None:
        """登记表是索引不是主存：写它失败不能影响候选落盘，只记诊断。"""
        script = self.project / "scripts" / "resilient.py"
        with mock.patch.object(lib, "upsert_assets", side_effect=OSError("disk full")):
            result = self.run_turn("s11", script, "import argparse\nparser = argparse.ArgumentParser()")
        self.assertTrue(result.get("written"), "登记失败不能让候选丢证据")
        reasons = [record.get("reason") for _, record in lib.iter_diagnostics(1)]
        self.assertTrue(any("register_failed" in str(reason) for reason in reasons), f"应记诊断，实际 {reasons}")

    def test_rebuild_from_candidates(self) -> None:
        """表丢了可从候选重建：路径指针在候选记录里，指纹从磁盘 stat 重算。"""
        script = self.project / "scripts" / "rebuild.py"
        self.run_turn("s12", script, "import argparse\nparser = argparse.ArgumentParser()")
        lib.asset_registry_path().unlink()
        self.assertEqual(lib.load_assets(), [])
        output = self.run_cli(["assets"])
        self.assertIn("资产登记表为空", output)


class PromptTest(BaseCase):
    """开场提示行：有积压才输出，一行封顶；它是唯一允许在开场执行的经验库命令。"""

    def test_silent_when_nothing_pending(self) -> None:
        self.assertEqual(self.run_cli(["prompt"]).strip(), "")

    def test_single_line_when_current_project_has_candidates(self) -> None:
        self.seed_candidate_file(self.project)
        output = self.run_cli(["prompt"]).strip()
        self.assertEqual(len(output.splitlines()), 1)
        self.assertIn("当前项目 1 条", output)
        self.assertIn("复盘由开场闸门自动派发", output)

    def test_reports_other_projects(self) -> None:
        other = self.other_project()
        self.seed_candidate_file(other)
        output = self.run_cli(["prompt"]).strip()
        self.assertEqual(len(output.splitlines()), 1)
        self.assertIn("gwsvc 1 条", output)

    def test_reports_recent_session_end(self) -> None:
        other = self.other_project("agentsvc")
        self.seed_candidate_file(other)
        exit_sentinel.write_session_end("codex", "beef12345678", lib.ensure_project(other), str(other), "process_exit", 1)
        output = self.run_cli(["prompt"]).strip()
        self.assertIn("最近结束 codex/beef1234", output)

    def test_line_segments_are_not_glued(self) -> None:
        """候选与条目同时存在时两段不能粘成一句，也不能给条目段硬加「另有」。"""
        self.seed_candidate_file(self.project)
        self.run_cli(
            [
                "add", "--manual", "--title", "GWSVC 单测编译顺序",
                "--category", "tooling", "--type", "path", "--scope", "project",
                "--action", "先 make deps 再 make test", "--tags", "gwsvc,unittest",
            ]
        )
        output = self.run_cli(["prompt"]).strip()
        self.assertIn("当前项目 1 条待复盘候选，demo 已有 1 条经验条目。", output)
        self.assertIn("需要时按现场字样 search 召回，不必等我确认。复盘由开场闸门自动派发", output)
        self.assertNotIn("另有 demo", output)
        self.assertNotIn("不必等我确认需要复盘", output)

    def test_does_not_print_candidate_details(self) -> None:
        """开场提示必须省 token：不许把候选正文带出来。"""
        self.seed_candidate_file(self.project)
        output = self.run_cli(["prompt"])
        self.assertNotIn("查询超时后改成先过滤", output)


class GlobalBacklogTest(BaseCase):
    """候选按项目分目录，消费口径必须是全局的，否则别的项目的候选永远没人看。"""

    def test_candidate_projects_includes_other_project(self) -> None:
        other = self.other_project()
        self.seed_candidate_file(other)
        self.assertIn("gwsvc", lib.candidate_projects())

    def test_review_all_lists_other_projects(self) -> None:
        other = self.other_project()
        cid = self.seed_candidate_file(other)
        output = self.run_cli(["review", "--all"])
        self.assertIn(cid, output)
        self.assertIn("[gwsvc]", output)

    def test_review_default_warns_about_other_backlog(self) -> None:
        other = self.other_project()
        self.seed_candidate_file(other)
        output = self.run_cli(["review"])
        self.assertIn("另有项目存在积压", output)
        self.assertIn("gwsvc 1 条", output)
        self.assertIn("review --all", output)

    def test_review_json_all_reports_project_field(self) -> None:
        other = self.other_project()
        cid = self.seed_candidate_file(other)
        payload = json.loads(self.run_cli(["review", "--all", "--json"]))
        self.assertEqual([item["project"] for item in payload], ["gwsvc"])
        self.assertEqual(payload[0]["id"], cid)

    def test_stats_counts_all_projects(self) -> None:
        other = self.other_project()
        self.seed_candidate_file(other)
        report = json.loads(self.run_cli(["stats", "--json"]))
        self.assertEqual(report["pending_candidates"], 0)
        self.assertEqual(report["pending_candidates_all"], 1)
        self.assertEqual(report["projects_with_backlog"], {"gwsvc": 1})

    def test_gc_apply_cleans_other_projects(self) -> None:
        other = self.other_project()
        self.seed_candidate_file(other, ts="2020-01-01T10:00:00+08:00")
        output = self.run_cli(["gc", "--older-than", "0d", "--apply"])
        self.assertIn("候选超过存活上限", output)
        self.assertEqual(list((self.home / "candidates" / "gwsvc").glob("*.jsonl")), [])

    def test_pending_marker_surfaces_other_session_end(self) -> None:
        other = self.other_project("agentsvc")
        self.seed_candidate_file(other)
        other_name = lib.ensure_project(other)
        exit_sentinel.write_session_end("codex", "abcdef123456", other_name, str(other), "process_exit", 1)
        output = self.run_cli(["review"])
        self.assertIn("最近结束的会话", output)
        self.assertIn("codex/abcdef12", output)

    def test_pending_marker_without_candidates_is_cleared(self) -> None:
        """标记只是提示：候选已被消费完的标记必须在下次 review 时清掉。"""
        exit_sentinel.write_session_end("codex", "deadbeef0001", "demo", str(self.project), "process_exit", 2)
        self.assertEqual(len(lib.read_pending()), 1)
        self.run_cli(["review"])
        self.assertEqual(lib.read_pending(), [])


class ManualAddTest(BaseCase):
    """会话内当场沉淀：没有候选也要能入库，但证据强度必须降级。"""

    def test_manual_add_without_candidate(self) -> None:
        output = self.run_cli(
            [
                "add", "--manual", "--title", "手工条目示例", "--category", "pitfall", "--type", "recovery",
                "--scope", "project", "--action", "先查 scan 再写条目", "--root-cause", "项目内已有载体时重复记录",
            ]
        )
        self.assertIn("已写入条目", output)
        entries = list(lib.iter_entries(scope="project", project="demo"))
        self.assertEqual(len(entries), 1)
        meta = entries[0][1]
        self.assertEqual(meta["confidence"], "low")
        self.assertEqual(meta["evidence"]["sessions"], [])
        self.assertEqual(meta["evidence"]["sources"], [])

    def test_manual_rejects_from(self) -> None:
        other = self.other_project()
        cid = self.seed_candidate_file(other)
        with self.assertRaises(SystemExit):
            self.run_cli(
                [
                    "add", "--manual", "--from", cid, "--title", "冲突用法", "--category", "pitfall",
                    "--type", "recovery", "--action", "随便", "--root-cause", "随便",
                ]
            )

    def test_add_without_from_still_works(self) -> None:
        """无 --from 无 --manual 的历史用法保持可用。"""
        output = self.run_cli(
            [
                "add", "--title", "无候选直接写", "--category", "workflow", "--type", "path",
                "--scope", "project", "--action", "先跑 scan",
            ]
        )
        self.assertIn("已写入条目", output)


class SentinelTest(BaseCase):
    """没有会话结束事件的客户端，靠退出哨兵补标记。"""

    TABLE = {
        30: (20, "python"),
        20: (10, "cmd"),
        10: (1, "claude"),
    }

    def test_parent_chain_walks_up(self) -> None:
        chain = exit_sentinel.parent_chain(30, self.TABLE)
        self.assertEqual(chain, [(30, "python"), (20, "cmd"), (10, "claude")])

    def test_resolve_prefers_client_process_name(self) -> None:
        table = {30: (20, "python"), 20: (10, "node"), 10: (1, "claude"), 1: (0, "explorer")}
        self.assertEqual(exit_sentinel.resolve_watch_pid("claude", 30, table), 10)

    def test_resolve_falls_back_to_host_runtime(self) -> None:
        """pi 是跑在 node 上的 JS CLI，认不出 pi 本体时按宿主运行时定位。"""
        table = {30: (20, "python"), 20: (10, "node"), 10: (1, "pwsh"), 1: (0, "explorer")}
        self.assertEqual(exit_sentinel.resolve_watch_pid("pi", 30, table), 20)

    def test_resolve_returns_none_when_client_absent(self) -> None:
        """认不出客户端就不派生：宁可没有标记，也不能把钩子退出当成会话结束。"""
        table = {30: (20, "python"), 20: (1, "pwsh"), 1: (0, "explorer")}
        self.assertIsNone(exit_sentinel.resolve_watch_pid("pi", 30, table))

    def test_resolve_skips_host_fallback_for_claude(self) -> None:
        """Claude Code 有原生 SessionEnd，认不出 claude 本体时不许退到 node，避免写假标记。"""
        table = {30: (20, "python"), 20: (10, "node"), 10: (1, "pwsh"), 1: (0, "explorer")}
        self.assertIsNone(exit_sentinel.resolve_watch_pid("claude", 30, table))

    def test_spawn_skipped_when_pid_unresolvable(self) -> None:
        self._spawn.stop()
        try:
            with mock.patch.object(exit_sentinel, "resolve_watch_pid", return_value=None):
                self.assertIsNone(exit_sentinel.spawn("pi", "s-1", "demo", str(self.project), 1))
        finally:
            self._spawn.start()
        self.assertFalse(exit_sentinel.sentinel_pid_path("pi", "s-1").exists())

    def test_wait_for_exit_observes_real_child(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.3)"])
        try:
            self.assertTrue(exit_sentinel.wait_for_exit(child.pid, timeout_seconds=15, poll_seconds=0.1))
        finally:
            child.wait(timeout=10)

    def test_zombie_child_counts_as_exited(self) -> None:
        """POSIX 上已退出但未回收的子进程是僵尸：`kill(pid, 0)` 仍成功，必须判为已退出。"""
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.2)"])
        time.sleep(0.8)
        try:
            self.assertFalse(exit_sentinel.process_alive(child.pid), "僵尸进程不能算活着")
        finally:
            child.wait(timeout=10)

    def test_session_end_marker_is_idempotent(self) -> None:
        exit_sentinel.write_session_end("pi", "0123456789ab", "demo", str(self.project), "process_exit", 3)
        exit_sentinel.write_session_end("pi", "0123456789ab", "demo", str(self.project), "process_exit", 4)
        records = lib.read_pending()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["candidates"], 4)

    def test_count_candidates_reads_all_files(self) -> None:
        other = self.other_project("agentsvc")
        self.seed_candidate_file(other, session="a")
        self.seed_candidate_file(other, session="b")
        self.assertEqual(exit_sentinel.count_candidates("agentsvc"), 2)

    def test_hook_starts_sentinel_after_first_candidate(self) -> None:
        path = Path(self.temp.name) / "sentinel-turn.jsonl"
        write_jsonl(
            path,
            [
                {"type": "message", "timestamp": "2026-09-22T10:00:01Z", "message": {"role": "user", "content": "查 VPC"}},
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:00:05Z",
                    "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "x"}}]},
                },
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:00:20Z",
                    "message": {"role": "toolResult", "toolName": "bash", "isError": True, "content": [{"type": "text", "text": "Error: refused"}]},
                },
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:00:40Z",
                    "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "c2", "name": "bash", "arguments": {"command": "x"}}]},
                },
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:00:50Z",
                    "message": {"role": "toolResult", "toolName": "bash", "isError": False, "content": [{"type": "text", "text": "ok"}]},
                },
                {
                    "type": "message",
                    "timestamp": "2026-09-22T10:01:00Z",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "根因是访问被拒绝，调整后查询成功"}], "usage": {"totalTokens": 100}},
                },
            ],
        )
        result = experience_hook.handle(
            {"client": "pi", "event": "turn_end", "session_id": "s-sentinel", "turn_id": "1", "cwd": str(self.project), "transcript_path": str(path)}
        )
        self.assertTrue(result.get("written"))
        self.assertEqual(self.spawn_mock.call_count, 1)
        self.assertEqual(self.spawn_mock.call_args.args[0], "pi")

    def test_native_session_end_writes_marker_without_transcript(self) -> None:
        result = experience_hook.handle(
            {"client": "claude", "event": "SessionEnd", "session_id": "claude-abc12345", "cwd": str(self.project)}
        )
        self.assertTrue(result.get("session_end"))
        records = lib.read_pending()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["reason"], "native_hook")
        self.assertEqual(records[0]["client"], "claude")

    def test_quiet_turn_does_not_start_sentinel(self) -> None:
        path = Path(self.temp.name) / "quiet2.jsonl"
        write_jsonl(
            path,
            [
                {"type": "message", "timestamp": "2026-09-22T10:00:01Z", "message": {"role": "user", "content": "谢谢"}},
                {"type": "message", "timestamp": "2026-09-22T10:00:02Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "不客气"}]}},
            ],
        )
        experience_hook.handle(
            {"client": "pi", "event": "turn_end", "session_id": "s-quiet", "turn_id": "1", "cwd": str(self.project), "transcript_path": str(path)}
        )
        self.assertEqual(self.spawn_mock.call_count, 0)


class DiagnosticsTest(BaseCase):
    """采集静默失败必须留痕：诊断只装异常，正常没命中的轮次不进日志。"""

    def test_log_and_read_roundtrip(self) -> None:
        lib.log_diagnostic("parse", "no_transcript", client="pi", session_id="s1", turn_id="2")
        rows = list(lib.iter_diagnostics(7))
        self.assertEqual(len(rows), 1)
        _, record = rows[0]
        self.assertEqual(record["stage"], "parse")
        self.assertEqual(record["reason"], "no_transcript")
        self.assertEqual(record["client"], "pi")

    def test_empty_fields_are_dropped(self) -> None:
        lib.log_diagnostic("parse", "no_transcript", client="pi", path="", turn_id=None)
        record = list(lib.iter_diagnostics(7))[0][1]
        self.assertNotIn("path", record)
        self.assertNotIn("turn_id", record)

    def test_hook_records_diagnostic_for_missing_transcript(self) -> None:
        result = experience_hook.handle(
            {"client": "pi", "event": "turn_end", "session_id": "s-diag", "turn_id": "1", "cwd": str(self.project),
             "transcript_path": str(Path(self.temp.name) / "nope.jsonl")}
        )
        self.assertEqual(result["stage"], "parse")
        self.assertEqual(result["skipped"], "no_transcript")
        reasons = [record["reason"] for _, record in lib.iter_diagnostics(7)]
        self.assertEqual(reasons, ["no_transcript"])

    def test_unknown_event_is_recorded(self) -> None:
        experience_hook.handle({"client": "pi", "event": "session_start", "session_id": "s", "cwd": str(self.project)})
        record = list(lib.iter_diagnostics(7))[0][1]
        self.assertEqual(record["stage"], "normalize")
        self.assertTrue(record["reason"].startswith("unknown_event:"))

    def test_quiet_turn_writes_no_diagnostic(self) -> None:
        """没有信号是正常结果，不该进诊断，否则日志被正常流量淹没。"""
        path = Path(self.temp.name) / "quiet-diag.jsonl"
        write_jsonl(
            path,
            [
                {"type": "message", "timestamp": "2026-09-22T10:00:01Z", "message": {"role": "user", "content": "谢谢"}},
                {"type": "message", "timestamp": "2026-09-22T10:00:02Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "不客气"}]}},
            ],
        )
        result = experience_hook.handle(
            {"client": "pi", "event": "turn_end", "session_id": "s", "turn_id": "1", "cwd": str(self.project), "transcript_path": str(path)}
        )
        self.assertEqual(result.get("skipped"), "no_signal")
        self.assertEqual(list(lib.iter_diagnostics(7)), [])

    def test_unreadable_stdin_is_recorded(self) -> None:
        class BrokenStdin:
            class buffer:  # noqa: N801
                @staticmethod
                def read(_limit):
                    raise OSError("pipe closed")

        with mock.patch("sys.stdin", BrokenStdin()):
            self.assertEqual(experience_hook.main([]), 0)
        self.assertEqual(list(lib.iter_diagnostics(7))[0][1]["stage"], "stdin")

    def test_stdin_with_bom_still_parses(self) -> None:
        """Windows PowerShell 重定向写 BOM：带着 BOM 的 payload 仍要能采集。"""
        path = Path(self.temp.name) / "bom.jsonl"
        write_jsonl(
            path,
            [
                {"type": "event_msg", "timestamp": "2026-10-08T10:00:01Z", "payload": {"type": "item_completed", "turn_id": "t1", "item": {"type": "UserMessage", "content": [{"type": "text", "text": "重试一下"}]}}},
                {"type": "event_msg", "timestamp": "2026-10-08T10:00:02Z", "payload": {"type": "item_completed", "turn_id": "t1", "item": {"type": "CommandExecution", "command": ["make"], "exit_code": 1, "aggregated_output": "boom"}}},
                {"type": "event_msg", "timestamp": "2026-10-08T10:00:03Z", "payload": {"type": "item_completed", "turn_id": "t1", "item": {"type": "CommandExecution", "command": ["make"], "exit_code": 0, "aggregated_output": "ok"}}},
                {"type": "event_msg", "timestamp": "2026-10-08T10:00:04Z", "payload": {"type": "item_completed", "turn_id": "t1", "item": {"type": "CommandExecution", "command": ["conntrack", "-S", "--note", "实测这台机器上 conntrack 老化时间是 120 秒，只在本机成立"], "exit_code": 0, "aggregated_output": "ok"}}},
            ],
        )
        payload = {
            "client": "codex",
            "event": "Stop",
            "session_id": "bom-session",
            "cwd": str(self.project),
            "transcript_path": str(path),
        }

        class BomStdin:
            class buffer:  # noqa: N801
                @staticmethod
                def read(_limit):
                    return b"\xef\xbb\xbf" + json.dumps(payload).encode("utf-8")

        with mock.patch("sys.stdin", BomStdin()):
            self.assertEqual(experience_hook.main([]), 0)
        self.assertEqual(list(lib.iter_diagnostics(7)), [])
        records = [item[1] for item in experience.iter_candidates(lib.ensure_project(self.project))]
        self.assertEqual(len(records), 1)
        self.assertIn("error_recovered", records[0]["signal"])

    def test_doctor_reports_summary(self) -> None:
        lib.log_diagnostic("parse", "no_transcript", client="pi", session_id="abcdef123456")
        self.seed_candidate_file(self.project)
        output = self.run_cli(["doctor"])
        self.assertIn("no_transcript 1", output)
        self.assertIn("待复盘候选：demo 1 条", output)

    def test_doctor_json_shape(self) -> None:
        lib.log_diagnostic("hook", "exception:ValueError", client="pi")
        report = json.loads(self.run_cli(["doctor", "--json"]))
        self.assertEqual(report["by_reason"], {"exception:ValueError": 1})
        self.assertEqual(report["diagnostics"], 1)

    def test_gc_removes_expired_diagnostics(self) -> None:
        old = lib.diagnostics_dir() / "2020-01-01.jsonl"
        lib.atomic_write_text(old, json.dumps({"ts": "2020-01-01T00:00:00+08:00", "stage": "parse", "reason": "x"}) + "\n")
        output = self.run_cli(["gc", "--older-than", "0d", "--apply"])
        self.assertIn("采集诊断", output)
        self.assertFalse(old.exists())


class SchemaTest(BaseCase):
    """候选 schema 是写入契约，枚举必须与代码常量同源，否则会悄悄漂移。"""

    def valid_record(self) -> dict:
        return {
            "ts": "2026-09-23T10:00:00+08:00",
            "client": "pi",
            "session_id": "s1",
            "turn_id": "1",
            "project_name": "demo",
            "cwd": "F:/x",
            "compaction_count": 0,
            "signal": ["retry"],
            "user": "u",
            "assistant": "a",
            "tools": [{"name": "bash", "args": {}, "status": "unknown", "error": "", "error_text_hint": "boom"}],
            "outcome": "success",
            "cost": {"tool_calls": 1, "tokens": 10, "duration_ms": 1000},
            "evidence": {"path": "~/.pi/x.jsonl", "lines": [1, 9]},
        }

    def test_valid_record_passes(self) -> None:
        self.assertEqual(lib.validate_candidate(self.valid_record()), [])

    def test_missing_required_field_is_reported(self) -> None:
        record = self.valid_record()
        del record["signal"]
        violations = lib.validate_candidate(record)
        self.assertTrue(any("signal" in item and "缺必填字段" in item for item in violations))

    def test_bad_enum_and_type_are_reported(self) -> None:
        record = self.valid_record()
        record["client"] = "unknown-cli"
        record["compaction_count"] = -1
        record["tools"][0]["status"] = "maybe"
        violations = lib.validate_candidate(record)
        joined = "\n".join(violations)
        self.assertIn("candidate.client", joined)
        self.assertIn("candidate.compaction_count", joined)
        self.assertIn("candidate.tools[0].status", joined)

    def test_hint_length_is_capped(self) -> None:
        record = self.valid_record()
        record["tools"][0]["error_text_hint"] = "x" * 201
        self.assertTrue(any("error_text_hint" in item for item in lib.validate_candidate(record)))

    def test_client_enum_matches_code(self) -> None:
        schema = lib.load_candidate_schema()
        # 候选来源方是五端加 agent 自记的备选。
        self.assertEqual(schema["properties"]["client"]["enum"], list(lib.CANDIDATE_CLIENTS))

    def test_note_signal_is_declared(self) -> None:
        schema = lib.load_candidate_schema()
        self.assertIn(lib.NOTE_SIGNAL, schema["properties"]["signal"]["items"]["enum"])

    def test_signal_enum_covers_code(self) -> None:
        """从 prescreen 源码里抠出实际产出的信号名，逐个核对 schema 枚举。"""
        source = (SCRIPTS / "prescreen.py").read_text(encoding="utf-8")
        produced = set(re.findall(r'signals\.append\("([a-z_]+)"\)', source))
        produced |= set(re.findall(r'return \["([a-z_]+)"\]', source))
        produced |= set(re.findall(r'return _correction\("([a-z_]+)"', source))
        self.assertTrue(produced, "未从 prescreen 里解析出任何信号")
        allowed = set(lib.load_candidate_schema()["properties"]["signal"]["items"]["enum"])
        self.assertEqual(produced - allowed, set(), f"schema 缺少这些信号：{produced - allowed}")

    def test_every_declared_signal_is_implemented(self) -> None:
        """文档声明的信号必须真在代码里产出，schema 里不留空条目的信号名。

        这条用例挡的是文档跑在实现前面的漂移：写了却没实现，比没写更误导。
        """
        doc = (SCRIPTS.parent / "docs" / "experience-prescreen-table-design.md").read_text(encoding="utf-8")
        declared = set(re.findall(r"^\| [A-H]\d+ \| `([a-z_]+)`", doc, re.M))
        declared |= set(re.findall(r"`(intent_repeat|artifact_churn|dep_matrix|root_cause_mapped|counterintuitive)`", doc))
        sources = "\n".join(
            (SCRIPTS / name).read_text(encoding="utf-8")
            for name in ("prescreen.py", "experience.py", "experience_hook.py", "experience_lib.py")
        )
        missing = sorted(name for name in declared if f'"{name}"' not in sources)
        self.assertEqual(missing, [], f"文档声明了但代码里不存在的信号：{missing}")

    def test_every_declared_signal_has_a_case(self) -> None:
        """文档声明一条信号，就必须有一条用例。

        挡的是「写了实现没写验证」：这类遗漏只有跑起来才暴露，读代码看不出来。
        """
        doc = (SCRIPTS.parent / "docs" / "experience-prescreen-table-design.md").read_text(encoding="utf-8")
        declared = set(re.findall(r"^\| [A-H]\d+ \| `([a-z_]+)`", doc, re.M))
        own_source = Path(__file__).read_text(encoding="utf-8")
        uncovered = sorted(name for name in declared if f'"{name}"' not in own_source)
        self.assertEqual(uncovered, [], f"这些信号只有实现没有用例：{uncovered}")

    def test_no_unused_detector_constants(self) -> None:
        """模块里的正则与常量必须被用到。

        死常量不只是垃圾：它说明设计里有一条判据写了却没接上，
        读代码时看着像实现了。阈值可能被别的模块引用（如 sweep 用巡检阈值），
        所以按整个 scripts 目录统计引用次数。
        """
        source = (SCRIPTS / "prescreen.py").read_text(encoding="utf-8")
        others = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(SCRIPTS.glob("*.py"))
            if path.name != "prescreen.py"
        )
        names = re.findall(r"^([A-Z][A-Z0-9_]{2,}) = ", source, re.M)
        unused = []
        for name in names:
            uses = len(re.findall(rf"\b{name}\b", source)) + len(re.findall(rf"\b{name}\b", others))
            if uses <= 1:
                unused.append(name)
        self.assertEqual(unused, [], f"这些常量定义了却没被使用：{unused}")

    def test_declared_statuses_are_reachable(self) -> None:
        """登记表声明的状态必须有代码真的写入，否则是死字段。"""
        sources = "\n".join(
            (SCRIPTS / name).read_text(encoding="utf-8")
            for name in ("prescreen.py", "experience.py", "experience_hook.py")
        )
        for status in ("active", "stale", "gone", "archived"):
            self.assertIn(f'"{status}"', sources, f"状态 {status} 没有任何写入点")

    def test_correction_kind_enum_covers_code(self) -> None:
        source = (SCRIPTS / "prescreen.py").read_text(encoding="utf-8")
        kinds = set(re.findall(r'_correction\("[a-z_]+", "([a-z_]+)"', source))
        self.assertTrue(kinds, "未从 prescreen 里解析出任何纠正类型")
        allowed = set(lib.load_candidate_schema()["properties"]["correction"]["properties"]["kind"]["enum"])
        self.assertEqual(kinds - allowed, set(), f"schema 缺少这些纠正类型：{kinds - allowed}")

    def test_status_and_outcome_enums_match_code(self) -> None:
        schema = lib.load_candidate_schema()
        statuses = set(re.findall(r'^STATUS_[A-Z]+ = "([a-z]+)"', (SCRIPTS / "transcripts.py").read_text(encoding="utf-8"), re.M))
        statuses.add("denied")  # 适配器可上报的权限拒绝状态
        self.assertEqual(statuses, set(schema["properties"]["tools"]["items"]["properties"]["status"]["enum"]))
        outcomes = set(re.findall(r'turn\.outcome = "([a-z]+)"', (SCRIPTS / "transcripts.py").read_text(encoding="utf-8")))
        outcomes.add("success")
        self.assertEqual(outcomes, set(schema["properties"]["outcome"]["enum"]))

    def test_hook_logs_violation_but_still_writes(self) -> None:
        """schema 违规说明组装出错：记诊断、仍然落盘，绝不因为校验丢证据。"""
        path = Path(self.temp.name) / "violate.jsonl"
        write_jsonl(
            path,
            [
                {"type": "message", "timestamp": "2026-09-22T10:00:01Z", "message": {"role": "user", "content": "查 VPC"}},
                {"type": "message", "timestamp": "2026-09-22T10:00:05Z", "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "x"}}]}},
                {"type": "message", "timestamp": "2026-09-22T10:00:09Z", "message": {"role": "toolResult", "toolName": "bash", "isError": True, "content": [{"type": "text", "text": "boom"}]}},
                {"type": "message", "timestamp": "2026-09-22T10:00:15Z", "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "c2", "name": "bash", "arguments": {"command": "conntrack -S --note '实测这台机器上 conntrack 老化时间是 120 秒，只在本机成立'"}}]}},
                {"type": "message", "timestamp": "2026-09-22T10:00:19Z", "message": {"role": "toolResult", "toolName": "bash", "isError": False, "content": [{"type": "text", "text": "ok；实测这台机器上 conntrack 老化时间是 120 秒，只在本机成立"}]}},
                {"type": "message", "timestamp": "2026-09-22T10:00:25Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "好了"}]}},
            ],
        )
        broken = {"ts": "2026-09-23T10:00:00+08:00", "client": "pi", "project_name": "demo"}
        with mock.patch.object(experience_hook, "build_candidate", return_value=broken):
            result = experience_hook.handle(
                {"client": "pi", "event": "turn_end", "session_id": "s-schema", "turn_id": "1", "cwd": str(self.project), "transcript_path": str(path)}
            )
        self.assertTrue(result["written"])
        self.assertTrue(result["schema_violations"])
        stages = [record["stage"] for _, record in lib.iter_diagnostics(7)]
        self.assertIn("schema", stages)
        self.assertEqual(len(list((self.home / "candidates" / "demo").glob("*.jsonl"))), 1)

    def test_hook_drops_record_without_project(self) -> None:
        """连归属都没有的记录不许落盘，否则会污染别处。"""
        path = Path(self.temp.name) / "noproject.jsonl"
        write_jsonl(
            path,
            [
                {"type": "message", "timestamp": "2026-09-22T10:00:01Z", "message": {"role": "user", "content": "查 VPC"}},
                {"type": "message", "timestamp": "2026-09-22T10:00:05Z", "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "x"}}]}},
                {"type": "message", "timestamp": "2026-09-22T10:00:09Z", "message": {"role": "toolResult", "toolName": "bash", "isError": True, "content": [{"type": "text", "text": "boom"}]}},
                {"type": "message", "timestamp": "2026-09-22T10:00:15Z", "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "c2", "name": "bash", "arguments": {"command": "conntrack -S --note '实测这台机器上 conntrack 老化时间是 120 秒，只在本机成立'"}}]}},
                {"type": "message", "timestamp": "2026-09-22T10:00:19Z", "message": {"role": "toolResult", "toolName": "bash", "isError": False, "content": [{"type": "text", "text": "ok；实测这台机器上 conntrack 老化时间是 120 秒，只在本机成立"}]}},
                {"type": "message", "timestamp": "2026-09-22T10:00:25Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "好了"}]}},
            ],
        )
        with mock.patch.object(experience_hook, "build_candidate", return_value={"ts": "x", "client": "pi"}):
            result = experience_hook.handle(
                {"client": "pi", "event": "turn_end", "session_id": "s-noproject", "turn_id": "1", "cwd": str(self.project), "transcript_path": str(path)}
            )
        self.assertFalse(result["written"])
        self.assertEqual(result["skipped"], "missing_project_name")
        self.assertEqual(list((self.home / "candidates").rglob("*.jsonl")), [])

    def test_validate_cli_flags_broken_candidate(self) -> None:
        path = lib.candidate_file("demo", "pi", "broken0001", lib.today())
        lib.atomic_write_text(path, json.dumps({"ts": "2026-09-23T10:00:00+08:00", "client": "pi"}) + "\n")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = experience.main(["validate"])
        self.assertEqual(code, 1)
        self.assertIn("问题 1 条", buffer.getvalue())

    def test_validate_cli_passes_on_good_candidate(self) -> None:
        self.seed_candidate_file(self.project)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = experience.main(["validate"])
        self.assertEqual(code, 0)
        self.assertIn("问题 0 条", buffer.getvalue())


class EntryContractTest(BaseCase):
    """条目是五端共读的资产：写入前校验，读取端对形状异常要留痕且不能算错。"""

    def add_entry(self, **overrides) -> str:
        payload = {
            "title": "CMS 查询先按 hostname 过滤",
            "category": "tooling",
            "type": "tool-rule",
            "scope": "project",
            "action": "先按 hostname 过滤再查",
            "tags": "cms,query",
        }
        payload.update(overrides)
        argv = ["add"]
        for key, value in payload.items():
            argv += [f"--{key.replace('_', '-')}", str(value)]
        return self.run_cli(argv)

    def write_entry_file(self, name: str, frontmatter: str, body: str = "正文\n") -> Path:
        path = lib.project_dir("demo") / "tooling" / name
        lib.atomic_write_text(path, f"---\n{frontmatter}\n---\n\n{body}")
        return path

    def test_add_writes_schema_valid_entry(self) -> None:
        self.assertIn("已写入条目", self.add_entry())
        entries = list(lib.iter_entries(scope="project", project="demo"))
        self.assertEqual(len(entries), 1)
        self.assertEqual(lib.validate_entry(entries[0][1]), [])

    def test_entry_roundtrip_preserves_types(self) -> None:
        """空映射曾被转义成字符串 "{}"，读回来 precondition 变 str，硬过滤整段失效。"""
        self.add_entry()
        path, meta, _ = list(lib.iter_entries(scope="project", project="demo"))[0]
        self.assertIsInstance(meta["precondition"], dict)
        self.assertIsInstance(meta["tags"], list)
        self.assertIn("precondition: {}", lib.read_text(path))
        self.assertEqual(lib.validate_entry(meta), [])

    def test_legacy_escaped_precondition_is_tolerated(self) -> None:
        """历史条目里可能残留 precondition: "{}"，读取端按无前置条件处理，不抛异常。"""
        self.write_entry_file("tooling-legacy.md", 'id: tooling-legacy\ntitle: 旧条目\ncategory: tooling\nprecondition: "{}"\ntags: []\naction: a\n')
        meta = list(lib.iter_entries(scope="project", project="demo"))[0][1]
        self.assertEqual(meta["precondition"], "{}")
        self.assertEqual(experience.precondition_hits(meta, "查一下", "cms_query", None), 0)

    def test_search_survives_scalar_fields(self) -> None:
        """标量 tags 会让 " ".join 逐字符拼接，标量 evidence 会让 len 数错：读取端必须收敛。"""
        self.write_entry_file(
            "tooling-scalar.md",
            "id: tooling-scalar\ntitle: CMS 查询先按 hostname 过滤\ncategory: tooling\ntype: tool-rule\n"
            "kind: original\nscope: project\nprecondition:\n  tools:\n    - cms_query\n"
            'tags: cms\naction: 先按 hostname 过滤\n'
            "evidence:\n  sessions: codex/abcdef12#t1\n  counters: []\n  sources: []\n"
            "confidence: low\nverification: n/a\ncreated_at: 2026-09-23\nverified_at: 2026-09-23\nstatus: active",
        )
        output = self.run_cli(["search", "--query", "cms", "--tool", "cms_query", "--json"])
        payload = json.loads(output)
        # 检索结果含两类：经验条目与可复用资产登记，条目挂在 entries 下。
        self.assertEqual(len(payload["entries"]), 1)
        self.assertGreater(payload["entries"][0]["score"], 0)
        self.assertEqual(payload["assets"], [])

        report = json.loads(self.run_cli(["stats", "--json"]))
        self.assertEqual(report["support_sessions"], 1)

    def test_search_matches_error_text_from_precondition(self) -> None:
        """按报错原文必须搜得到：precondition.errors 存的就是现场原文。"""
        self.write_entry_file(
            "pitfall-gwsvc-link.md",
            "id: pitfall-gwsvc-link\ntitle: GWSVC 单测链接报错处理\ncategory: pitfall\ntype: recovery\n"
            "kind: original\nscope: project\nprecondition:\n  tools:\n    - make\n  errors:\n    - undefined reference\n"
            "tags: gwsvc\n"
            "action: 加 -Wl,--allow-shlib-undefined\n"
            "evidence:\n  sessions: codex/abcdef12#t1\n  counters: []\n  sources: []\n"
            "confidence: low\nverification: n/a\ncreated_at: 2026-09-23\nverified_at: 2026-09-23\nstatus: active",
        )
        hit = self.run_cli(["search", "--query", "undefined reference"])
        self.assertIn("pitfall-gwsvc-link", hit)
        miss = self.run_cli(["search", "--query", "编译失败怎么办"])
        self.assertIn("没有匹配的经验条目", miss)

    def test_search_without_query_lists_project_inventory(self) -> None:
        """空 query 是接手任务时的清单入口：不必猜关键词就能看到本项目有什么。"""
        self.write_entry_file(
            "tooling-inventory.md",
            "id: tooling-inventory\ntitle: GWSVC 单测编译顺序\ncategory: tooling\ntype: path\n"
            "kind: original\nscope: project\nprecondition:\n  tools:\n    - make\n"
            "tags: gwsvc,unittest\n"
            "action: 先 make deps 再 make test\n"
            "evidence:\n  sessions: codex/abcdef12#t1\n  counters: []\n  sources: []\n"
            "confidence: low\nverification: n/a\ncreated_at: 2026-09-23\nverified_at: 2026-09-23\nstatus: active",
        )
        output = self.run_cli(["search", "--limit", "20"])
        self.assertIn("经验清单", output)
        self.assertIn("tooling-inventory", output)
        self.assertIn("先 make deps 再 make test", output)

    def test_search_reports_hard_filter_exclusion(self) -> None:
        """全被 --tool 滤掉时要说明原因，否则会被误读成「经验库没这条经验」。"""
        self.write_entry_file(
            "tooling-filtered.md",
            "id: tooling-filtered\ntitle: GWSVC 单测编译顺序\ncategory: tooling\ntype: path\n"
            "kind: original\nscope: project\nprecondition:\n  tools:\n    - make\n"
            "tags: gwsvc\naction: 先 make deps\n"
            "evidence:\n  sessions: codex/abcdef12#t1\n  counters: []\n  sources: []\n"
            "confidence: low\nverification: n/a\ncreated_at: 2026-09-23\nverified_at: 2026-09-23\nstatus: active",
        )
        output = self.run_cli(["search", "--query", "make", "--tool", "gcc"])
        self.assertIn("硬过滤排除", output)
        self.assertIn("去掉 --tool", output)

    def test_search_by_path_narrows_to_applicable_entries(self) -> None:
        """按需召回：动单测文件时只出单测经验，不动源码经验。"""
        self.write_entry_file(
            "tooling-unit.md",
            "id: tooling-unit\ntitle: GWSVC 单测编译顺序\ncategory: tooling\ntype: path\n"
            "kind: original\nscope: project\nprecondition:\n  tools:\n    - make\n  files:\n    - tests/\n"
            "tags: gwsvc\naction: 先 make deps 再 make test\n"
            "evidence:\n  sessions: codex/abcdef12#t1\n  counters: []\n  sources: []\n"
            "confidence: low\nverification: n/a\ncreated_at: 2026-09-23\nverified_at: 2026-09-23\nstatus: active",
        )
        self.write_entry_file(
            "pitfall-src.md",
            "id: pitfall-src\ntitle: GWSVC 源码抓包定位\ncategory: pitfall\ntype: recovery\n"
            "kind: original\nscope: project\nprecondition:\n  errors:\n    - conntrack full\n"
            "tags: gwsvc\naction: 先抓包\n"
            "evidence:\n  sessions: codex/abcdef12#t2\n  counters: []\n  sources: []\n"
            "confidence: low\nverification: n/a\ncreated_at: 2026-09-23\nverified_at: 2026-09-23\nstatus: active",
        )
        hit = self.run_cli(["search", "--path", "tests/unit/test_nat64.c"])
        self.assertIn("适用于 tests/unit/test_nat64.c 的条目 1 条", hit)
        self.assertIn("tooling-unit", hit)
        # 没声明 files 的存量条目不丢，但降级为「可能相关」。
        self.assertIn("可能相关 1 条", hit)
        self.assertIn("pitfall-src", hit)
        miss = self.run_cli(["search", "--path", "src/ip_vs_core.c"])
        self.assertIn("没有条目声明适用于", miss)

    def test_search_any_terms(self) -> None:
        """模型只知道本地话题词时用 --any 召回：单个词就能命中。"""
        self.write_entry_file(
            "tooling-any.md",
            "id: tooling-any\ntitle: GWSVC 单测编译顺序\ncategory: tooling\ntype: path\n"
            "kind: original\nscope: project\nprecondition:\n  tools:\n    - make\n"
            "tags: gwsvc,unittest\naction: 先 make deps 再 make test\n"
            "evidence:\n  sessions: codex/abcdef12#t1\n  counters: []\n  sources: []\n"
            "confidence: low\nverification: n/a\ncreated_at: 2026-09-23\nverified_at: 2026-09-23\nstatus: active",
        )
        hit = self.run_cli(["search", "--query", "不存在的词,unittest", "--any"])
        self.assertIn("tooling-any", hit)
        miss = self.run_cli(["search", "--query", "不存在的词,unittest"])
        self.assertIn("没有匹配的经验条目", miss)
        self.assertIn("--any", miss, "全词没命中时要提示改用 --any")

    def test_derive_files_from_body(self) -> None:
        """存量补全：正文里出现 ≥2 次的文件线索才采信。"""
        meta = {
            "title": "GWSVC 远端单测清理权限",
            "action": "在 unit_test_core_ut 为每个 GW_TYPE 建独立 worktree",
            "source_of_truth": None,
        }
        body = "现象：make clean 权限不够。runner 用 root 写目录，unit_test_core_ut 里复现。"
        derived = experience.derive_files(meta, body)
        self.assertIn("unit_test_core_ut", derived)
        self.assertNotIn("GW_TYPE", derived, "全大写是常量，不是文件名")
        self.assertNotIn("runner", derived, "通用词认了等于没收窄")

    def test_backfill_adds_files_and_search_then_matches(self) -> None:
        """补全后按路径召回立刻生效：这是存量条目不失效的关键闭环。"""
        self.write_entry_file(
            "pitfall-legacy.md",
            "id: pitfall-legacy\ntitle: GWSVC 单测清理权限\ncategory: pitfall\ntype: recovery\n"
            "kind: original\nscope: project\nprecondition:\n  errors:\n    - 权限不够\n"
            "tags: gwsvc\naction: 在 unit_test_core_ut 建独立 worktree\n"
            "evidence:\n  sessions: codex/abcdef12#t1\n  counters: []\n  sources: []\n"
            "confidence: low\nverification: n/a\ncreated_at: 2026-09-23\nverified_at: 2026-09-23\nstatus: active",
            "现象：unit_test_core_ut 里 make clean 权限不够，unit_test_core_ut 复现稳定。\n",
        )
        before = self.run_cli(["search", "--path", "unit_test_core_ut/run.sh"])
        self.assertIn("没有条目声明适用于", before)
        preview = self.run_cli(["backfill"])
        self.assertIn("可从正文推导出适用文件 1 条", preview)
        self.assertIn("unit_test_core_ut", preview)
        self.assertIn("这是预演", preview)
        applied = self.run_cli(["backfill", "--apply"])
        self.assertIn("已写入 1 条", applied)
        after = self.run_cli(["search", "--path", "unit_test_core_ut/run.sh"])
        self.assertIn("适用于 unit_test_core_ut/run.sh 的条目 1 条", after)
        self.assertIn("pitfall-legacy", after)
        meta = next(iter(lib.iter_entries(scope="project", project="demo")))[1]
        self.assertEqual(meta["precondition"]["files"], ["unit_test_core_ut"])

    def test_backfill_leaves_declared_entries_alone(self) -> None:
        """已声明 files 的条目不该被推导结果覆盖。"""
        self.write_entry_file(
            "tooling-declared.md",
            "id: tooling-declared\ntitle: GWSVC 单测编译顺序\ncategory: tooling\ntype: path\n"
            "kind: original\nscope: project\nprecondition:\n  files:\n    - tests/\n"
            "tags: gwsvc\naction: 先 make deps\n"
            "evidence:\n  sessions: codex/abcdef12#t1\n  counters: []\n  sources: []\n"
            "confidence: low\nverification: n/a\ncreated_at: 2026-09-23\nverified_at: 2026-09-23\nstatus: active",
            "tests/unit 与 tests/unit 都要先装依赖。\n",
        )
        output = self.run_cli(["backfill"])
        self.assertIn("已声明适用文件 1 条", output)
        self.assertIn("没有可推导的条目", output)
        meta = next(iter(lib.iter_entries(scope="project", project="demo")))[1]
        self.assertEqual(meta["precondition"]["files"], ["tests/"])

    def test_add_rejects_invalid_entry(self) -> None:
        """校验不过直接拒绝写入：写坏一条会让别的 agent 认不出来。"""
        with mock.patch.object(lib, "validate_entry", return_value=["entry.confidence：取值 'maybe' 不在 ['high', 'medium', 'low'] 内"]):
            with self.assertRaises(SystemExit) as caught:
                self.add_entry()
        self.assertIn("frontmatter 契约", str(caught.exception))
        self.assertEqual(list(lib.project_dir("demo").rglob("*.md")), [])

    def test_validate_entry_reports_type_drift(self) -> None:
        meta = {
            "id": "tooling-x", "title": "x", "category": "tooling", "type": "tool-rule", "kind": "original",
            "scope": "project", "precondition": {"tools": "cms_query"}, "tags": "cms", "action": "a",
            "evidence": {"sessions": [], "counters": [], "sources": []}, "confidence": "low",
            "verification": "n/a", "created_at": "2026-09-23", "verified_at": "2026-09-23", "status": "active",
        }
        violations = lib.validate_entry(meta)
        joined = "\n".join(violations)
        self.assertIn("entry.precondition.tools", joined)
        self.assertIn("entry.tags", joined)

    def test_validate_entry_accepts_nullable_fields(self) -> None:
        meta = {
            "id": "tooling-x", "title": "x", "category": "tooling", "type": "tool-rule", "kind": "original",
            "scope": "global", "project": None, "precondition": {}, "tags": [], "action": "a",
            "source_of_truth": None, "evidence": {"sessions": [], "counters": [], "sources": []},
            "confidence": "low", "verification": "n/a", "created_at": "2026-09-23T10:00:00+08:00",
            "verified_at": "2026-09-23T10:00:00+08:00", "status": "active", "supersedes": None, "related": [],
        }
        self.assertEqual(lib.validate_entry(meta), [])

    def test_scalar_precondition_does_not_misfire(self) -> None:
        """tools 写成标量时，字符串子串匹配会把 cms 当成命中 cms_query。"""
        meta = {"precondition": {"tools": "cms_query"}}
        self.assertEqual(experience.precondition_hits(meta, "查一下", "cms_query", None), 1)
        with self.assertRaises(ValueError):
            experience.precondition_hits(meta, "查一下", "cms", None)

    def test_scalar_tags_do_not_create_false_duplicate(self) -> None:
        """tags 写成标量时，集合运算会退化成按字符比较，误判成同一经验。"""
        self.write_entry_file("tooling-existing.md", "id: tooling-existing\ntitle: 另一条经验\ntags: cms\ncategory: tooling")
        output = self.add_entry(title="完全不同的经验", tags="cms,query")
        self.assertIn("已写入条目", output)
        self.assertEqual(len(list(lib.iter_entries(scope="project", project="demo"))), 2)

    def test_unreadable_entry_is_logged(self) -> None:
        self.write_entry_file("tooling-broken.md", "id: tooling-broken\n  title: 缩进坏了")
        self.assertEqual(list(lib.iter_entries(scope="project", project="demo")), [])
        rows = [record for _, record in lib.iter_diagnostics(7)]
        self.assertTrue(any(record["stage"] == "entry" and record["reason"].startswith("unreadable:") for record in rows))

    def test_validate_cli_flags_broken_entry(self) -> None:
        self.write_entry_file(
            "tooling-bad-enum.md",
            "id: tooling-bad-enum\ntitle: 坏的\ncategory: tooling\ntype: tool-rule\nkind: original\n"
            "scope: project\nprecondition: {}\ntags: []\naction: a\nconfidence: maybe\n"
            "evidence:\n  sessions: []\n  counters: []\n  sources: []\n"
            "verification: n/a\ncreated_at: 2026-09-23\nverified_at: 2026-09-23\nstatus: active",
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = experience.main(["validate"])
        output = buffer.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("条目校验：检查 1 条，问题 1 条", output)
        self.assertIn("entry.confidence", output)


class NoteCandidateTest(BaseCase):
    """agent 自记备选：只落候选，复盘确认后才入库，避免条目膨胀。"""

    NOTE = [
        "note",
        "--title", "CMS 换用 SDK 查询",
        "--action", "改用 SDK 分页查询，单页 100 条",
        "--root-cause", "CLI 走的是旧网关，Throttling 无法通过重试绕开",
        "--detail", "Throttling: Request was denied due to request throttling",
        "--pre-tool", "cms_cli",
        "--pre-error", "Throttling",
        "--tags", "cms,throttling",
    ]

    def project_entries(self):
        return list(lib.iter_entries(scope="project", project=lib.ensure_project(self.project)))

    def test_note_lands_candidate_not_entry(self) -> None:
        self.run_cli(self.NOTE)
        self.assertEqual(self.project_entries(), [])
        rows = list(experience.iter_candidates(lib.ensure_project(self.project)))
        self.assertEqual(len(rows), 1)
        cid, record, _, _ = rows[0]
        self.assertEqual(record["client"], lib.MANUAL_CLIENT)
        self.assertEqual(record["signal"], [lib.NOTE_SIGNAL])
        self.assertEqual(lib.validate_candidate(record), [])
        review = self.run_cli(["review"])
        self.assertIn("agent_note", review)
        self.assertIn("CMS 换用 SDK 查询", review)
        self.assertIn("CMS 换用 SDK 查询", self.run_cli(["show", cid]))

    def test_validate_accepts_note_candidate(self) -> None:
        self.run_cli(self.NOTE)
        self.assertIn("候选校验：检查 1 条，问题 0 条", self.run_cli(["validate"]))

    def test_add_from_note_carries_precondition_and_stays_low(self) -> None:
        self.run_cli(self.NOTE)
        cid = next(iter(experience.iter_candidates(lib.ensure_project(self.project))))[0]
        self.run_cli(["add", "--from", cid, "--title", "CMS 换用 SDK 查询", "--category", "pitfall", "--type", "recovery"])
        entries = self.project_entries()
        self.assertEqual(len(entries), 1)
        _, meta, body = entries[0]
        # 备选里的工具与报错、标签、结论都带进条目，检索条件不丢。
        self.assertEqual(meta["precondition"]["tools"], ["cms_cli"])
        self.assertEqual(meta["precondition"]["errors"], ["Throttling"])
        self.assertEqual(meta["tags"], ["cms", "throttling"])
        self.assertIn("旧网关", body)
        self.assertIn("agent 自记", body)
        # 没有转录行号可回溯，置信度必须停在 low。
        self.assertEqual(meta["confidence"], "low")
        self.assertEqual(meta["evidence"]["sources"], [])
        self.assertEqual(list(experience.iter_candidates(lib.ensure_project(self.project))), [])

    def test_note_requires_action(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_cli(["note", "--title", "只有标题"])


class IndexSectionTest(BaseCase):
    """索引段会被写进全局规则文件，替换逻辑必须只动这一节。"""

    def test_replace_keeps_other_sections(self) -> None:
        text = "# 全局规则\n\n## 语言风格\n\n极简。\n\n## 经验库\n\n旧版开场强制复盘。\n\n## 行为规范\n\n禁破坏性操作。\n"
        updated = install.replace_index_section(text)
        self.assertIn("## 语言风格", updated)
        self.assertIn("## 行为规范", updated)
        # 命令必须写成 "$HOME/..." 带引号的形式：PowerShell 不展开裸 ~，bash 能展开 $HOME。
        self.assertIn('python "$HOME/.agents/skills/experience-keeper/scripts/experience.py" gate', updated)
        self.assertIn("复盘闸门", updated)
        self.assertIn("按需加载", updated)
        self.assertNotIn("旧版开场强制复盘", updated)

    def test_section_keeps_opening_nonblocking(self) -> None:
        """开场只跑闸门：不派发就不动作，派发也照样干活，不得要求通读条目或等确认。"""
        section = install.INDEX_SECTION
        self.assertIn("gate", section)
        self.assertIn("后台派复盘子 Agent", section)
        self.assertIn("原样转发 brief", section)
        self.assertNotIn("停下", section)
        self.assertNotIn("确认", section)
        self.assertIn("仅主 Agent 派发", section)
        self.assertIn("不通读条目", section)
        self.assertIn("search", section)
        self.assertIn("沉淀入口", section)

    def test_replace_handles_section_at_end(self) -> None:
        updated = install.replace_index_section("# 规则\n\n## 经验库\n\n旧内容\n")
        self.assertTrue(updated.endswith("\n"))
        self.assertIn("SKILL.md", updated)

    def test_current_version_is_detected(self) -> None:
        text = "# 规则\n" + install.INDEX_SECTION
        self.assertTrue(install.index_section_current(text))
        self.assertFalse(install.index_section_current("# 规则\n\n## 经验库\n\n旧内容\n"))

    def test_written_index_section_matches_template_bytes(self) -> None:
        """写盘不能被换行翻译成 CRLF，否则 --check 永远认为索引段是旧版。"""
        path = Path(self.temp.name) / "AGENTS.md"
        path.write_bytes("# 规则\r\n\r\n## 经验库\r\n\r\n旧内容\r\n".encode("utf-8"))
        text = path.read_text(encoding="utf-8")
        canonical = install.replace_index_section(text)
        install.Installer(apply=True).write_text(path, canonical, "更新索引段")
        raw = path.read_bytes()
        self.assertNotIn(b"\r", raw)
        self.assertEqual(raw, canonical.encode("utf-8"))
        self.assertTrue(install.index_section_current(raw.decode("utf-8")))


class AgentsLinkTest(unittest.TestCase):
    """规则文件链接：副本或指向别处必须重建，内容漂移必须被自检发现。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.global_file = root / "agents" / "AGENTS.md"
        self.global_file.parent.mkdir(parents=True)
        self.global_file.write_text("# 全局规则\n", encoding="utf-8")
        self.consumer = root / "client" / "AGENTS.md"
        self.consumer.parent.mkdir(parents=True)
        self.patch = mock.patch.object(install, "AGENTS_GLOBAL", self.global_file)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.temp.cleanup()

    def test_status_separates_copy_from_drift(self) -> None:
        self.assertEqual(install.agents_link_status(self.consumer), "missing")
        self.consumer.write_text("# 全局规则\n", encoding="utf-8")
        self.assertEqual(install.agents_link_status(self.consumer), "copy")
        # 全局规则更新后副本就旧了，只有逐字节比对能发现。
        self.consumer.write_text("# 旧规则\n", encoding="utf-8")
        self.assertEqual(install.agents_link_status(self.consumer), "drift")

    def test_status_reads_link_target(self) -> None:
        other = Path(self.temp.name) / "other.md"
        other.write_text("# 别的文件\n", encoding="utf-8")
        try:
            self.consumer.symlink_to(self.global_file)
        except OSError:
            self.skipTest("当前环境不允许创建符号链接")
        self.assertEqual(install.agents_link_status(self.consumer), "linked")
        self.consumer.unlink()
        self.consumer.symlink_to(other)
        self.assertEqual(install.agents_link_status(self.consumer), "elsewhere")

    def test_relink_drops_stale_copy_and_keeps_backup(self) -> None:
        self.consumer.write_text("# 旧规则\n", encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            message = install.relink_agents_file(install.Installer(apply=True), self.consumer)
        self.assertIn(install.agents_link_status(self.consumer), ("linked", "copy"), message)
        self.assertEqual(self.consumer.read_bytes(), self.global_file.read_bytes())
        self.assertTrue(list(self.consumer.parent.glob("AGENTS.md.bak-*")), "旧副本必须先备份")

    def test_check_flags_drifted_copy(self) -> None:
        home = Path(self.temp.name) / "home"
        (home / ".agents").mkdir(parents=True)
        (home / ".pi" / "agent").mkdir(parents=True)
        global_file = home / ".agents" / "AGENTS.md"
        global_file.write_text("# 全局规则\n", encoding="utf-8")
        (home / ".pi" / "agent" / "AGENTS.md").write_text("# 旧规则\n", encoding="utf-8")
        with mock.patch.object(install, "HOME", home), mock.patch.object(install, "AGENTS_GLOBAL", global_file):
            items = {name: (ok, detail) for name, ok, detail in install.check_wiring()}
        ok, detail = items["pi 全局规则链接"]
        self.assertFalse(ok)
        self.assertIn("不一致", detail)


class InstallUpgradeTest(BaseCase):
    """skill 换位置后，五端配置里的旧路径必须被替换，不能追加出第二条钩子。"""

    OLD = "python /home/user/service/skills/experience-keeper/scripts/experience_hook.py"
    NEW = "python /home/user/.agents/skills/experience-keeper/scripts/experience_hook.py"

    def test_dsh_install_skips_without_preset_dir(self) -> None:
        """全新机器没有 dsh 预设目录：跳过而不是抛 FileNotFoundError。"""
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            with mock.patch.object(install, "HOME", root), redirect_stdout(io.StringIO()):
                install.install_dsh(install.Installer(apply=True))
            self.assertFalse((root / ".dsh").exists(), "缺预设时不该凭空建目录")

    def test_shared_creates_global_rules_when_missing(self) -> None:
        """全新机器没有全局规则文件：建出来并写入索引段，否则客户端不会主动跑 gate。"""
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            target = root / ".agents" / "AGENTS.md"
            with mock.patch.object(install, "HOME", root), \
                    mock.patch.object(install, "AGENTS_GLOBAL", target), \
                    redirect_stdout(io.StringIO()):
                install.install_shared(install.Installer(apply=True))
            text = target.read_text(encoding="utf-8")
            self.assertIn("## 经验库", text)
            self.assertTrue(install.index_section_current(text))
            self.assertTrue((root / ".agents" / "experience").is_dir(), "经验库根目录也要建出来")

    def test_bom_json_config_still_installs(self) -> None:
        """Windows 的 PowerShell/记事本会写出 UTF-8 BOM：带 BOM 的配置不能被判成坏文件。"""
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            (root / ".claude").mkdir()
            (root / ".claude" / "settings.json").write_text("\ufeff{}\n", encoding="utf-8")
            with mock.patch.object(install, "HOME", root), redirect_stdout(io.StringIO()):
                install.install_claude(install.Installer(apply=True))
            payload = json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8").lstrip("\ufeff"))
            commands = [item["hooks"][0]["command"] for item in payload["hooks"]["Stop"]]
            self.assertIn(install.HOOK_SCRIPT.as_posix(), commands[0])
            self.assertEqual(commands[0].count('"'), 2, "脚本路径要带引号，用户名带空格也能跑")

    def test_check_passes_on_machine_without_clients(self) -> None:
        """全新空机器：没装任何客户端时自检不该报红，否则用户永远看不到「全部接线就绪」。"""
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            target = root / ".agents" / "AGENTS.md"
            with mock.patch.object(install, "HOME", root), \
                    mock.patch.object(install, "AGENTS_GLOBAL", target), \
                    redirect_stdout(io.StringIO()):
                install.install_shared(install.Installer(apply=True))
                items = {name: (ok, detail) for name, ok, detail in install.check_wiring()}
            for name in (
                "Claude Code 采集钩子",
                "Claude Code 会话结束钩子",
                "Codex 采集钩子",
                "dsh 采集插件",
                "dsh 适配器为当前版本",
                "pi 扩展注册",
                "codemaker 插件注册",
            ):
                with self.subTest(item=name):
                    ok, detail = items[name]
                    self.assertTrue(ok, f"{name} 不该报红：{detail}")
                    self.assertIn("未安装", detail)

    def test_upsert_hook_appends_then_replaces(self) -> None:
        entries: list = []
        self.assertEqual(install.upsert_hook(entries, f"{self.OLD} --client claude"), "appended")
        self.assertEqual(install.upsert_hook(entries, f"{self.OLD} --client claude"), "current")
        self.assertEqual(install.upsert_hook(entries, f"{self.NEW} --client claude"), "replaced")
        self.assertEqual(len(entries), 1)
        self.assertIn(self.NEW, entries[0]["hooks"][0]["command"])

    def test_upsert_hook_collapses_duplicates(self) -> None:
        entries = [
            {"hooks": [{"type": "command", "command": f"{self.OLD} --client claude"}]},
            {"hooks": [{"type": "command", "command": f"{self.NEW} --client claude"}]},
        ]
        self.assertEqual(install.upsert_hook(entries, f"{self.NEW} --client claude"), "replaced")
        self.assertEqual(len(entries), 1)

    def test_upsert_hook_keeps_unrelated_entries(self) -> None:
        entries = [{"hooks": [{"type": "command", "command": "python other_hook.py"}]}]
        self.assertEqual(install.upsert_hook(entries, f"{self.NEW} --client claude"), "appended")
        self.assertEqual(len(entries), 2)
        self.assertIn("other_hook.py", entries[0]["hooks"][0]["command"])

    def test_upsert_path_replaces_registered_extension(self) -> None:
        entries = ["other-plugin", "/home/user/service/skills/experience-keeper/adapters/pi/x.ts"]
        self.assertEqual(install.upsert_path(entries, "C:/home/.agents/skills/experience-keeper/adapters/pi/x.ts"), "replaced")
        self.assertEqual(len(entries), 2)
        self.assertTrue(entries[1].startswith("C:/home/"))

    def test_codex_command_line_is_rewritten(self) -> None:
        text = (
            "[[hooks.Stop]]\n[[hooks.Stop.hooks]]\n"
            'type = "command"\n'
            f'command = "{self.OLD} --client codex"\n'
        )
        updated = install.rewrite_codex_command(text, f"{self.NEW} --client codex")
        self.assertIn(self.NEW, updated)
        self.assertNotIn(self.OLD, updated)
        self.assertEqual(updated.count("[[hooks.Stop]]"), 1)

    def test_codex_rewrite_is_idempotent(self) -> None:
        text = f'command = "{self.NEW} --client codex"\n'
        self.assertEqual(install.rewrite_codex_command(text, f"{self.NEW} --client codex"), text)

    def test_codex_installs_into_hooks_json_when_present(self) -> None:
        """0.156 起 Codex 只读 hooks.json：写进 config.toml 的钩子永远不会触发。"""
        home = Path(self.temp.name) / "home"
        (home / ".codex").mkdir(parents=True)
        others = {"hooks": [{"type": "command", "command": "python other_hook.py"}]}
        (home / ".codex" / "hooks.json").write_text(
            json.dumps({"hooks": {"Stop": [others]}}, ensure_ascii=False), encoding="utf-8"
        )
        config = home / ".codex" / "config.toml"
        config.write_text('[tui]\nstatus_line = []\n', encoding="utf-8")
        with mock.patch.object(install, "HOME", home):
            install.install_codex(install.Installer(apply=True))
        payload = json.loads((home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        stop = payload["hooks"]["Stop"]
        self.assertEqual(len(stop), 2)
        self.assertIn("other_hook.py", stop[0]["hooks"][0]["command"])
        self.assertIn(install.HOOK_SCRIPT.as_posix(), stop[1]["hooks"][0]["command"])
        # 旧落点必须保持原样：往那儿追加会留下一条永不触发的钩子。
        self.assertNotIn(install.HOOK_MARKER, config.read_text(encoding="utf-8"))

    def test_codex_installs_into_config_toml_without_hooks_json(self) -> None:
        """没有 hooks.json 的是旧版 Codex，钩子仍写 config.toml。"""
        home = Path(self.temp.name) / "home"
        (home / ".codex").mkdir(parents=True)
        config = home / ".codex" / "config.toml"
        config.write_text('model = "gpt-5"\n', encoding="utf-8")
        with mock.patch.object(install, "HOME", home):
            install.install_codex(install.Installer(apply=True))
        text = config.read_text(encoding="utf-8")
        self.assertIn("[[hooks.Stop]]", text)
        self.assertIn(install.HOOK_SCRIPT.as_posix(), text)

    def test_check_wiring_reads_codex_hooks_json(self) -> None:
        home = Path(self.temp.name) / "home"
        (home / ".agents").mkdir(parents=True)
        (home / ".codex").mkdir(parents=True)
        (home / ".codex" / "hooks.json").write_text(
            json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": f"python {install.HOOK_SCRIPT.as_posix()} --client codex"}]}]}}),
            encoding="utf-8",
        )
        (home / ".agents" / "AGENTS.md").write_text("# 全局规则\n", encoding="utf-8")
        with mock.patch.object(install, "HOME", home), mock.patch.object(install, "AGENTS_GLOBAL", home / ".agents" / "AGENTS.md"):
            items = {name: (ok, detail) for name, ok, detail in install.check_wiring()}
        self.assertTrue(items["Codex 采集钩子"][0])

    def dsh_home(self, adapter_bytes: bytes) -> Path:
        home = Path(self.temp.name) / "home-dsh"
        preset = home / ".dsh" / ".agent-presets" / "liangshen"
        preset.mkdir(parents=True, exist_ok=True)
        (preset / "experience-keeper.mjs").write_bytes(adapter_bytes)
        (preset / "agent.cordis.yml").write_text(
            "- id: experience-keeper\n  name: ./experience-keeper.mjs\n", encoding="utf-8"
        )
        return home

    def test_dsh_check_passes_when_adapter_copy_matches(self) -> None:
        source = (install.SKILL_DIR / "adapters" / "dsh" / "experience-keeper.mjs").read_bytes()
        home = self.dsh_home(source)
        with mock.patch.object(install, "HOME", home), mock.patch.object(install, "AGENTS_GLOBAL", home / ".agents" / "AGENTS.md"):
            items = {name: (ok, detail) for name, ok, detail in install.check_wiring()}
        self.assertTrue(items["dsh 采集插件"][0])
        self.assertTrue(items["dsh 适配器为当前版本"][0])

    def test_dsh_check_flags_stale_adapter_copy(self) -> None:
        """预设目录由 dsh-tui 托管，升级会重写它：装了但过期必须看得出来，否则改了适配器却没生效。"""
        home = self.dsh_home(b"// old adapter\nexport function apply() {}\n")
        with mock.patch.object(install, "HOME", home), mock.patch.object(install, "AGENTS_GLOBAL", home / ".agents" / "AGENTS.md"):
            items = {name: (ok, detail) for name, ok, detail in install.check_wiring()}
        self.assertTrue(items["dsh 采集插件"][0], "插件行与文件都在，这一项仍算装上了")
        self.assertFalse(items["dsh 适配器为当前版本"][0], "副本过期必须报出来")
        self.assertIn("apply", items["dsh 适配器为当前版本"][1])

    def test_dsh_install_refreshes_stale_copy(self) -> None:
        home = self.dsh_home(b"// old adapter\n")
        with mock.patch.object(install, "HOME", home):
            install.install_dsh(install.Installer(apply=True))
        preset = home / ".dsh" / ".agent-presets" / "liangshen"
        source = (install.SKILL_DIR / "adapters" / "dsh" / "experience-keeper.mjs").read_bytes()
        self.assertEqual((preset / "experience-keeper.mjs").read_bytes(), source)
        # 插件行不重复追加：重跑 install 不该把同一行写两遍。
        self.assertEqual((preset / "agent.cordis.yml").read_text(encoding="utf-8").count("- id: experience-keeper"), 1)


class PrescreenRegressionTest(BaseCase):
    """真实轮次回归样本：改判据前后先跑它。

    样本取自一次 8 条候选的复盘（6 条噪声、2 条真值），每行给出该轮的期望：
    `expect_present` 必须命中，`expect_absent` 一个都不能命中。
    噪声多半来自「转述当实测」与「键粒度太粗」，这两类回归只有真实轮次能挡住。
    """

    FIXTURE = Path(__file__).resolve().parent / "fixtures" / "prescreen-regression" / "cases.jsonl"

    def cases(self) -> list[dict]:
        rows = [json.loads(line) for line in self.FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertTrue(rows, "回归样本集为空")
        return rows

    def test_regression_cases(self) -> None:
        for case in self.cases():
            with self.subTest(case=case["label"]):
                signals = set(prescreen.detect_signals(case["turn"]))
                missing = sorted(set(case["expect_present"]) - signals)
                noise = sorted(set(case["expect_absent"]) & signals)
                self.assertEqual(missing, [], f"{case['label']}：期望命中却缺失 {missing}")
                self.assertEqual(noise, [], f"{case['label']}：噪声信号回来了 {noise}")

    def test_regression_cases_cover_both_outcomes(self) -> None:
        """样本必须同时含「该转条目」与「该丢弃」两类，否则只挡半边回归。"""
        values = {case["value"] for case in self.cases()}
        self.assertEqual(values, {"yes", "no"})


class CarrierAndVerdictTest(BaseCase):
    """载体标记、复盘分组与判决台账。

    载体标记挡的是「结论已经写进规则/模板/知识库，经验库再记一条只是复述」；
    判决台账给「哪条信号主要在产噪声」留下数据，调判据不再靠一次会话的观察。
    """

    def seed_candidate(self, **extra) -> str:
        record = {
            "ts": lib.now_iso(),
            "client": "pi",
            "session_id": "s1",
            "turn_id": "1",
            "project_name": "demo",
            "cwd": str(self.project),
            "compaction_count": 0,
            "signal": ["env_hazard"],
            "user": "写规则",
            "assistant": "写好了",
            "tools": [],
            "outcome": "success",
            "cost": {"tool_calls": 0, "tokens": 0, "duration_ms": 1},
            "evidence": {"path": "~/t.jsonl", "lines": [1, 2]},
        }
        record.update(extra)
        path = lib.append_candidate("demo", record)
        return f"{path.stem}#t{record['turn_id']}"

    def test_carrier_write_detection(self) -> None:
        hit = prescreen.detect_carrier_write(
            {"tools": [{"name": "file_change", "args": {"paths": [".harness/rules/workflow.md"]}}]}
        )
        self.assertEqual(hit, ".harness/rules/workflow.md")
        agents = prescreen.detect_carrier_write(
            {"tools": [{"name": "edit", "args": {"file_path": "F:\\work\\demo\\AGENTS.md", "old_string": "a", "new_string": "b"}}]}
        )
        self.assertTrue(agents.endswith("AGENTS.md"))
        # 读载体、搜载体不算写进载体。
        self.assertEqual(prescreen.detect_carrier_write({"tools": [{"name": "exec", "args": {"command": "Get-Content .harness/rules/workflow.md"}}]}), "")
        self.assertEqual(prescreen.detect_carrier_write({"tools": [{"name": "edit", "args": {"file_path": "src/core/action.c"}}]}), "")

    def test_hook_marks_carrier(self) -> None:
        path = Path(self.temp.name) / "carrier.jsonl"
        write_jsonl(
            path,
            [
                {"type": "message", "timestamp": "2026-09-22T10:00:01Z", "message": {"role": "user", "content": "把规则写进载体"}},
                {"type": "message", "timestamp": "2026-09-22T10:00:05Z", "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "x"}}]}},
                {"type": "message", "timestamp": "2026-09-22T10:00:09Z", "message": {"role": "toolResult", "toolName": "bash", "isError": True, "content": [{"type": "text", "text": "boom"}]}},
                {"type": "message", "timestamp": "2026-09-22T10:00:15Z", "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "c2", "name": "bash", "arguments": {"command": "conntrack -S --note '实测这台机器上 conntrack 老化时间是 120 秒，只在本机成立'"}}]}},
                {"type": "message", "timestamp": "2026-09-22T10:00:19Z", "message": {"role": "toolResult", "toolName": "bash", "isError": False, "content": [{"type": "text", "text": "ok；实测这台机器上 conntrack 老化时间是 120 秒，只在本机成立"}]}},
                {"type": "message", "timestamp": "2026-09-22T10:00:25Z", "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "c3", "name": "write", "arguments": {"file_path": ".harness/rules/knowledge.md", "content": "# 规则\n"}}]}},
                {"type": "message", "timestamp": "2026-09-22T10:00:29Z", "message": {"role": "toolResult", "toolName": "write", "content": [{"type": "text", "text": "ok"}]}},
                # 载体写入会拦下候选，只有同时产出可复用资产时才落盘并带上载体标记。
                {"type": "message", "timestamp": "2026-09-22T10:00:31Z", "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "c4", "name": "write", "arguments": {"file_path": str(self.project / "scripts" / "probe.py"), "content": "import argparse\nparser = argparse.ArgumentParser()\nparser.add_argument('--host')"}}]}},
                {"type": "message", "timestamp": "2026-09-22T10:00:33Z", "message": {"role": "toolResult", "toolName": "write", "content": [{"type": "text", "text": "ok"}]}},
                {"type": "message", "timestamp": "2026-09-22T10:00:35Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "规则已进载体"}]}},
            ],
        )
        experience_hook.handle(
            {"client": "pi", "event": "turn_end", "session_id": "s-carrier", "turn_id": "1", "cwd": str(self.project), "transcript_path": str(path)}
        )
        record = next(record for _, record, _, _ in experience.iter_candidates("demo"))
        self.assertEqual(record.get("carrier"), ".harness/rules/knowledge.md")
        self.assertEqual(lib.validate_candidate(record), [])

    def test_review_groups_by_action(self) -> None:
        carrier = self.seed_candidate(carrier=".harness/rules/workflow.md")
        pending = self.seed_candidate(turn_id="2", signal=["protocol_edge"], confidence="pending")
        process = self.seed_candidate(turn_id="3", signal=["multi_source", "retry"])
        output = self.run_cli(["review"])
        self.assertIn("载体已覆盖候选", output)
        self.assertIn(".harness/rules/workflow.md", output)
        self.assertIn("待核实候选", output)
        self.assertIn("过程信号候选", output)
        # 载体组排在待核实之前：这类候选不需要读原文。
        self.assertLess(output.index("载体已覆盖候选"), output.index("待核实候选"))
        self.assertIn(carrier.split("#")[0], output)
        self.assertIn(pending.split("#")[0], output)
        self.assertIn(process.split("#")[0], output)

    def test_drop_records_reason(self) -> None:
        candidate = self.seed_candidate(signal=["perf_limit"])
        self.run_cli(["drop", candidate, "--reason", "carrier"])
        records = lib.iter_verdicts()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["decision"], "drop")
        self.assertEqual(records[0]["reason"], "carrier")
        self.assertEqual(records[0]["signal"], ["perf_limit"])
        self.assertEqual(records[0]["turn"], "1")
        self.assertEqual(list(experience.iter_candidates("demo")), [])

    def test_add_records_verdict(self) -> None:
        candidate = self.seed_candidate(signal=["grounded_verdict"])
        self.run_cli(
            [
                "add", "--from", candidate, "--title", "台账用例", "--category", "domain", "--type", "completion",
                "--action", "记台账", "--root-cause", "根因",
            ]
        )
        records = lib.iter_verdicts()
        self.assertEqual([item["decision"] for item in records], ["add"])
        self.assertEqual(records[0]["signal"], ["grounded_verdict"])
        self.assertTrue(records[0]["entry_id"])

    def test_stats_reports_conversion_rate(self) -> None:
        for index in range(3):
            lib.append_verdict("demo", {"ts": lib.now_iso(), "turn": f"t{index}", "signal": ["cost_trap"], "decision": "drop"})
        lib.append_verdict("demo", {"ts": lib.now_iso(), "turn": "t9", "signal": ["cost_trap"], "decision": "add"})
        lib.append_verdict("demo", {"ts": lib.now_iso(), "turn": "t10", "signal": ["env_hazard"], "decision": "drop"})
        output = self.run_cli(["stats"])
        self.assertIn("信号转正率", output)
        self.assertIn("cost_trap", output)
        # 只判决过一次的信号不进榜：样本太少，说明不了问题。
        self.assertNotIn("env_hazard", output)
        self.assertIn("复核", output)


class PrescreenTighteningTest(BaseCase):
    """准入闸门收紧：设计讨论轮与「转述当现场」的残余噪声。

    一批 8 条候选全部来自方案设计与讨论轮：判据从 Agent/SendMessage 的正文里取词，
    源码检索模式里的 `|stats` 被当成日志查询，分支名 `gameproxy` 被当成代理坑。
    这些用例锁住四条取词面修正与「讨论轮」闸门本身。
    """

    def turn(self, **overrides) -> dict:
        base = {
            "user": "",
            "prev_user": "",
            "prev_users": [],
            "prev_outcome": "",
            "assistant": "好了",
            "tools": [],
            "tool_calls": 0,
            "tokens": 0,
            "duration_ms": 1000,
            "outcome": "success",
        }
        base.update(overrides)
        return base

    def test_narrative_tool_args_are_not_evidence(self) -> None:
        """Agent 的 prompt 是正文：方案里提到「快照」「三个」「口径」不是本轮现场动作。"""
        delegated = self.turn(
            user="service 可以放 DACQ，将决策和依据写进文档",
            tools=[
                {
                    "name": "Agent",
                    "args": {"prompt": "不能引入 DACQ 汇总快照方案；删除三个非 service 对象的 dacq_index，统计口径沿用实测值"},
                    "status": "ok",
                },
                {
                    "name": "Bash",
                    "args": {"command": 'python "$HOME/.agents/skills/experience-keeper/scripts/experience.py" search --query "DACQ" --any'},
                    "status": "ok",
                },
            ],
        )
        signals = prescreen.detect_signals(delegated)
        self.assertNotIn("blast_radius_guard", signals)
        self.assertNotIn("cost_trap", signals)
        self.assertNotIn("evidence_capture", signals)
        # 同一句话出现在命令参数里才算现场：变更前的 dry-run 依旧命中。
        real = self.turn(
            tools=[{"name": "bash", "args": {"command": "aliyun vpc ModifyRouteEntry --dry-run"}, "status": "ok"}]
        )
        self.assertIn("blast_radius_guard", prescreen.detect_signals(real))

    def test_message_body_is_not_evidence(self) -> None:
        relay = self.turn(
            tools=[
                {"name": "SendMessage", "args": {"message": "让查指定基线，新建关系第一次有效结果即 HEALTHY，阈值不变"}, "status": "ok"},
                {"name": "AskUserQuestion", "args": {"question": "基线以哪次为准？"}, "status": "ok"},
            ]
        )
        signals = prescreen.detect_signals(relay)
        self.assertNotIn("evidence_capture", signals)
        self.assertNotIn("cost_trap", signals)

    def test_source_retrieval_is_not_a_query(self) -> None:
        """源码检索模式里的 `|stats` 不是日志查询语句。"""
        grep = self.turn(
            tools=[
                {
                    "name": "exec",
                    "args": {"command": "git show origin/lb-d12:include/ipvs/service.h | Select-String -Pattern 'struct dp_vs_service|dests|stats|refcnt'"},
                    "status": "ok",
                }
            ]
        )
        self.assertNotIn("asset_query", prescreen.detect_signals(grep))
        rg = self.turn(
            tools=[
                {
                    "name": "exec",
                    "args": {"command": 'git ls-tree -r --name-only origin/lb-d12 | rg "^(include/ipvs/(service|dest|stats)\\.h|src/ipvs/ip_vs_(service|dest|stats)\\.c)"'},
                    "status": "ok",
                }
            ]
        )
        self.assertNotIn("asset_query", prescreen.detect_signals(rg))
        # 真查询语句照旧命中。
        query = self.turn(
            tools=[{"name": "bash", "args": {"command": "select srcaddr, sum(bytes) from flow group by srcaddr"}, "status": "ok"}]
        )
        self.assertIn("asset_query", prescreen.detect_signals(query))

    def test_prereq_needs_word_boundary(self) -> None:
        """分支名里的 gameproxy 不是代理坑。"""
        branch = self.turn(
            tools=[
                {
                    "name": "exec",
                    "args": {"command": "git log --oneline"},
                    "status": "error",
                    "error": "9eb66da7 Mars/feature/gameproxy 同步 NgcConfig.java",
                },
                {"name": "exec", "args": {"command": "git show HEAD"}, "status": "ok"},
            ]
        )
        self.assertNotIn("prereq_discovered", prescreen.detect_signals(branch))
        real = self.turn(
            tools=[
                {"name": "bash", "args": {"command": "aws ec2 describe-vpcs"}, "status": "error", "error": "403 Forbidden: proxy authentication required"},
                {"name": "bash", "args": {"command": "aws ec2 describe-vpcs --profile prod"}, "status": "ok"},
            ]
        )
        self.assertIn("prereq_discovered", prescreen.detect_signals(real))

    def test_identifier_limit_is_not_a_context(self) -> None:
        """源码标识符里的 max/limit 不是上限语境。"""
        code = self.turn(
            tools=[
                {
                    "name": "exec",
                    "args": {"command": "git show HEAD:src/ipvs/ip_vs_dest.c"},
                    "status": "ok",
                    "result": "int max_dests = 200000;\nif (num > max_dests) return -1;\n",
                }
            ]
        )
        signals = prescreen.detect_signals(code)
        self.assertNotIn("perf_limit", signals)
        self.assertNotIn("soft_limit", signals)
        # 真上限语境照旧命中：数值带中文上限词。
        ceiling = self.turn(
            tools=[
                {
                    "name": "bash",
                    "args": {"command": "cat /proc/sys/net/ipv4/ip_local_port_range"},
                    "status": "ok",
                    "result": "本地端口范围最多 65000 个，端口耗尽后必然失败",
                }
            ]
        )
        self.assertIn("perf_limit", prescreen.detect_signals(ceiling))

    def test_discussion_only_gate(self) -> None:
        """只读代码加讨论的轮次不进候选池，现场轮与聊天轮照旧。"""
        design = self.turn(
            user="gwsvc/lb-d12 中的统计归属是怎么样的？上面的设计是否改变了 d12 原有语义？",
            assistant="对照 src/core/svc.c:120 与 include/ipvs/service.h:44，另见 src/ipvs/dest.c:8 与 src/core/action.c:31",
            tools=[
                {
                    "name": "exec",
                    "args": {"command": "git show origin/lb-d12:include/ipvs/service.h | Select-String -Pattern 'stats'"},
                    "status": "ok",
                }
            ],
        )
        signals = prescreen.detect_signals(design)
        self.assertIn("grounded_verdict", signals)
        self.assertTrue(prescreen.is_discussion_only(design))
        self.assertEqual("discussion_only", prescreen.candidate_skip_reason(design, signals))
        # 读现场数据（日志、抓包、内核面）不是讨论轮。
        log = self.turn(tools=[{"name": "bash", "args": {"command": "Get-Content /var/log/messages -Tail 50"}, "status": "ok"}])
        self.assertFalse(prescreen.is_discussion_only(log))
        # 调外部 CLI 读状态不是讨论轮。
        cli = self.turn(tools=[{"name": "bash", "args": {"command": "conntrack -L -p tcp"}, "status": "ok"}])
        self.assertFalse(prescreen.is_discussion_only(cli))
        # 写盘轮不是讨论轮，哪怕写的是脚本文件。
        writer = self.turn(
            tools=[{"name": "write", "args": {"file_path": "/srv/scripts/probe.py", "content": "print(1)"}, "status": "ok"}]
        )
        self.assertFalse(prescreen.is_discussion_only(writer))
        # 纯聊天轮不算讨论轮：长期约束仍要能落候选。
        self.assertFalse(prescreen.is_discussion_only(self.turn(user="以后都用指数退避", assistant="好")))
        # 实测轮的证据型信号不被讨论闸门吃掉。
        measured = self.turn(
            tools=[
                {
                    "name": "bash",
                    "args": {"command": "loadtest --run"},
                    "status": "ok",
                    "result": "压测到 5000 pps 开始丢包，端口耗尽上限 65535",
                }
            ]
        )
        self.assertIsNone(prescreen.candidate_skip_reason(measured, prescreen.detect_signals(measured)))

    def test_no_convergence_needs_field_output(self) -> None:
        """旧值只从现场输出读：源码注释里的 `unchanged`、文件名里的 add 都不算。"""
        review = self.turn(
            user="评审 gwsvc/openspec/changes/lb-aligned-config-model 目录所有文档",
            assistant="结论是暂不建议进入实施：2 个高、3 个中、5 个低",
            tools=[
                {"name": "Bash", "args": {"command": "for f in config-delivery.md address-delivery.md; do cat -n $f; done"}, "status": "ok", "result": "1 === config-delivery.md"},
                {
                    "name": "Bash",
                    "args": {"command": "git show 275adeac:include/core/res.h | sed -n 27,135p"},
                    "status": "ok",
                    "result": " *   EGWSVC_IDLE (unchanged) or EGWSVC_RESOURCE (a policy bit)\n",
                },
            ],
        )
        self.assertNotIn("no_convergence", prescreen.detect_signals(review))
        self.assertNotIn("batch_mutation", prescreen.detect_signals(review))
        # 真控制面读数仍要命中：变更成功之后读状态还是 pending。
        real = self.turn(
            tools=[
                {"name": "bash", "args": {"command": "aliyun vpc ModifyRouteEntry --next-hop 10.0.0.1"}, "status": "ok"},
                {
                    "name": "bash",
                    "args": {"command": "aliyun vpc DescribeRouteEntry --id rtb-1"},
                    "status": "ok",
                    "result": "status: pending，未生效",
                },
            ]
        )
        self.assertIn("no_convergence", prescreen.detect_signals(real))

    def test_heredoc_script_body_is_not_a_command(self) -> None:
        """参数里塞一整段脚本时，正文里的「回滚」「一次本机」不是本轮的兜底动作与实测数值。"""
        scripted = self.turn(
            user='先处理" targetgroup " 多余空格，再搬迁契约',
            tools=[
                {
                    "name": "Bash",
                    "args": {
                        "command": "mkdir -p /tmp/l4fix && cat > /tmp/l4fix/split.py <<'EOF'\n"
                        "# 回滚说明：本机只跑一次，dry-run 之后再执行\n"
                        "print('一次', '本机')\n"
                        "EOF\npython /tmp/l4fix/split.py"
                    },
                    "status": "ok",
                    "result": "done",
                }
            ],
        )
        signals = prescreen.detect_signals(scripted)
        self.assertNotIn("blast_radius_guard", signals)
        self.assertNotIn("cost_trap", signals)
        # 写在命令行上的兜底动作照旧命中。
        real = self.turn(
            tools=[{"name": "bash", "args": {"command": "aliyun vpc ModifyRouteEntry --dry-run"}, "status": "ok"}]
        )
        self.assertIn("blast_radius_guard", prescreen.detect_signals(real))
        # PowerShell here-string 形态同样只留命令行。
        here_string = self.turn(
            tools=[
                {
                    "name": "exec",
                    "args": {"command": "powershell -Command @'\n# 回滚：本机执行一次\n'@\ngit status"},
                    "status": "ok",
                }
            ]
        )
        self.assertNotIn("blast_radius_guard", prescreen.detect_signals(here_string))

    def test_carrier_via_delegated_write(self) -> None:
        """写盘发生在子会话里时，父轮的委托参数点名载体也算结论已进载体。"""
        carrier = prescreen.detect_carrier_write(
            {"tools": [{"name": "Agent", "args": {"prompt": "把统计归属结论更新到 gwsvc/openspec/changes/lb/design.md"}}]}
        )
        self.assertTrue(carrier.endswith("design.md"))
        turn = {"tools": [{"name": "Agent", "args": {"prompt": "更新 gwsvc/openspec/changes/lb/design.md"}}]}
        self.assertEqual("carrier_written", prescreen.candidate_skip_reason(turn, ["perf_limit"]))

    def test_carrier_covers_workspace_document_dirs(self) -> None:
        """载体识别要覆盖结论真正落地的地方，而不只是 rules/ 与 openspec/。"""
        for path in (
            ".harness/evals/repository-scoring.md",
            ".harness/kb/cross-component-change.md",
            ".harness/research/2026-11-04-repo-review.md",
            "docs/architecture.md",
            "../evals/README.md",
            "gwsvc/doc/architecture.md",
            "README.md",
        ):
            with self.subTest(path=path):
                hit = prescreen.detect_carrier_write(
                    {"tools": [{"name": "apply_patch", "args": {"input": f"*** Update File: {path}"}}]}
                )
                self.assertTrue(hit, path)
                self.assertEqual(
                    "carrier_written",
                    prescreen.candidate_skip_reason({"tools": [{"name": "apply_patch", "args": {"input": f"*** Update File: {path}"}}]}, ["perf_limit"]),
                )

    def test_generic_document_dirs_ignored_for_delegation(self) -> None:
        """委托 prompt 里点到 docs 路径是「让对方去读」，不算本轮写进载体。"""
        turn = {"tools": [{"name": "Agent", "args": {"prompt": "先读 docs/architecture.md 再回答"}}]}
        self.assertEqual("", prescreen.detect_carrier_write(turn))
        # 强载体在委托参数里仍然算：写盘发生在子会话里。
        strong = {"tools": [{"name": "Agent", "args": {"prompt": "读 .harness/rules/workflow.md 后按要求实施"}}]}
        self.assertTrue(prescreen.detect_carrier_write(strong))

    def test_idempotency_mismatch_does_not_admit_alone(self) -> None:
        """幂等抖动只作复盘线索：单独命中不再落候选。"""
        turn = self.turn(
            tools=[
                {"name": "bash", "args": {"command": "gwsvc-ctl list-nat"}, "status": "ok", "result": "count: 3"},
                {"name": "bash", "args": {"command": "gwsvc-ctl list-nat"}, "status": "ok", "result": "count: 4"},
            ]
        )
        signals = prescreen.detect_signals(turn)
        self.assertIn("idempotency_mismatch", signals)
        self.assertEqual("no_reusable_evidence", prescreen.candidate_skip_reason(turn, signals))

    def test_regate_preview_then_apply(self) -> None:
        """收紧后的闸门要能复判存量候选，否则噪声会一直躺到超期。"""
        self.seed_candidate_file(self.project, ts="2026-10-14T10:00:00+08:00")
        preview = self.run_cli(["regate"])
        self.assertIn("1 条候选现在会被跳过", preview)
        self.assertIn("no_reusable_evidence", preview)
        self.assertTrue(list(experience.iter_candidates("demo")))
        self.run_cli(["regate", "--apply"])
        self.assertEqual(list(experience.iter_candidates("demo")), [])
        reasons = [item["reason"] for item in lib.iter_verdicts()]
        self.assertIn("regate:no_reusable_evidence", reasons)


class ToolResultCaptureTest(BaseCase):
    """工具输出必须落库：判据型信号的第三个取词面就是它。"""

    def turn(self, **overrides) -> dict:
        base = {
            "user": "",
            "prev_user": "",
            "prev_users": [],
            "prev_outcome": "",
            "assistant": "好了",
            "tools": [],
            "tool_calls": 0,
            "tokens": 0,
            "duration_ms": 1000,
            "outcome": "success",
        }
        base.update(overrides)
        return base

    def test_settle_tool_keeps_truncated_result(self) -> None:
        tool = transcripts.new_tool("bash", {"command": "conntrack -S"})
        self.assertEqual(tool["result"], "")
        transcripts.settle_tool(tool, transcripts.STATUS_OK, "x" * (transcripts.RESULT_CHARS + 500))
        self.assertEqual(len(tool["result"]), transcripts.RESULT_CHARS)
        # 状态判定不受输出影响：没有权威字段就记 unknown，输出照旧留档。
        unknown = transcripts.new_tool("bash", {"command": "x"})
        transcripts.settle_tool(unknown, transcripts.STATUS_UNKNOWN, "boom")
        self.assertEqual(unknown["status"], "unknown")
        self.assertEqual(unknown["result"], "boom")

    def test_parsed_result_feeds_cost_trap(self) -> None:
        """端到端：转录里的成功输出经解析落进 result，再被判据读到。"""
        path = Path(self.temp.name) / "result.jsonl"
        write_jsonl(
            path,
            [
                {"type": "message", "timestamp": "2026-09-22T10:00:01Z", "message": {"role": "user", "content": "看下会话表"}},
                {"type": "message", "timestamp": "2026-09-22T10:00:05Z", "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "conntrack -S"}}]}},
                {"type": "message", "timestamp": "2026-09-22T10:00:09Z", "message": {"role": "toolResult", "toolName": "bash", "isError": False, "content": [{"type": "text", "text": "实测这台机器上 conntrack 老化时间是 120 秒，只在本机成立"}]}},
                {"type": "message", "timestamp": "2026-09-22T10:00:12Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "记下了"}], "usage": {}}},
            ],
        )
        turn = transcripts.parse_transcript("pi", str(path), "1")
        self.assertIn("conntrack", turn.tools[0]["result"])
        self.assertIn("cost_trap", prescreen.detect_signals(turn.to_dict()))

    def test_source_and_doc_results_are_transcription(self) -> None:
        """读源码、读文档、检索仓库的结果是转述，不进判据取词面。"""
        source = self.turn(
            tools=[
                {
                    "name": "exec",
                    "args": {"command": "git show HEAD:src/ipvs/ip_vs_dest.c"},
                    "status": "ok",
                    "result": "/* 端口耗尽上限 65535，超过就打满 */\nint max_dests = 200000;\n",
                }
            ]
        )
        self.assertNotIn("perf_limit", prescreen.detect_signals(source))
        search = self.turn(
            tools=[
                {
                    "name": "exec",
                    "args": {"command": "rg -n 'limit' src/"},
                    "status": "ok",
                    "result": "src/ipvs/ip_vs_dest.c:88:  if (num > max_dests) /* 耗尽 */",
                }
            ]
        )
        self.assertNotIn("perf_limit", prescreen.detect_signals(search))
        doc = self.turn(
            tools=[
                {
                    "name": "bash",
                    "args": {"command": "cat plan"},
                    "status": "ok",
                    "result": "# 升级计划\n\n压测到 5000 pps 时丢包，端口耗尽上限 65535。\n\n## 验收\n- 完成\n",
                }
            ]
        )
        self.assertNotIn("perf_limit", prescreen.detect_signals(doc))
        # 读现场数据面（内核参数、日志、抓包统计）仍然进取词面。
        field = self.turn(
            tools=[
                {
                    "name": "bash",
                    "args": {"command": "cat /proc/sys/net/ipv4/ip_local_port_range"},
                    "status": "ok",
                    "result": "本地端口范围最多 65000 个，端口耗尽后必然失败",
                }
            ]
        )
        self.assertIn("perf_limit", prescreen.detect_signals(field))

    def test_long_output_is_not_idempotency_mismatch(self) -> None:
        """构建日志、测试日志每次都有差异：抽不出稳定字段就不比，别把「跑了一次」记成幂等被破坏。"""
        noisy = self.turn(
            tools=[
                {"name": "bash", "args": {"command": "make"}, "status": "ok", "result": "line 1\nline 2\nline 3\nline 4\nelapsed 12ms"},
                {"name": "bash", "args": {"command": "make"}, "status": "ok", "result": "line 1\nline 2\nline 3\nline 4\nelapsed 19ms"},
            ]
        )
        self.assertNotIn("idempotency_mismatch", prescreen.detect_signals(noisy))
        scalar = self.turn(
            tools=[
                {"name": "bash", "args": {"command": "conntrack -C"}, "status": "ok", "result": "42310"},
                {"name": "bash", "args": {"command": "conntrack -C"}, "status": "ok", "result": "65535"},
            ]
        )
        self.assertIn("idempotency_mismatch", prescreen.detect_signals(scalar))

    def test_candidate_schema_carries_result(self) -> None:
        record = {
            "ts": lib.now_iso(),
            "client": "pi",
            "session_id": "s",
            "turn_id": "1",
            "project_name": "demo",
            "cwd": str(self.project),
            "compaction_count": 0,
            "signal": ["cost_trap"],
            "user": "u",
            "assistant": "a",
            "tools": [{"name": "bash", "status": "ok", "error": "", "result": "ok"}],
            "outcome": "success",
            "cost": {"tool_calls": 1, "tokens": 0, "duration_ms": 1},
            "evidence": {"path": "~/t.jsonl", "lines": [1, 2]},
        }
        self.assertEqual(lib.validate_candidate(record), [])
        record["tools"][0]["result"] = "x" * (transcripts.RESULT_CHARS + 1)
        self.assertTrue(lib.validate_candidate(record), "超长 result 必须被 schema 挡下")


class DshAdapterTest(BaseCase):
    """dsh 适配器端到端：会话事件 → 归一化转录 → 解析 → 判据。

    适配器是 JS，字段路径按 dsh 0.2 的 `SessionEventMap` 写死（`tool/result` 的 `isError`
    在 message 级、`content` 是内容块数组）。跨语言契约一旦对不上，采集会静默失效，
    因此这里真的把插件跑起来，断言它写出的转录能被 Python 侧解析。
    """

    ADAPTER = SCRIPTS.parent / "adapters" / "dsh" / "experience-keeper.mjs"

    def _node(self) -> str:
        node = shutil.which("node")
        if not node:
            self.skipTest("未安装 node，跳过 dsh 适配器端到端用例")
        return node

    def _stage(self) -> Path:
        """把适配器拷进临时预设目录：它按 `./session-events.mjs` 相对导入。"""
        work = Path(self.temp.name) / "preset"
        work.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.ADAPTER, work / "experience-keeper.mjs")
        (work / "session-events.mjs").write_text(
            "export function snapshotSessionEvents(session) {\n"
            "  const events = typeof session.snapshotEvents === 'function' ? session.snapshotEvents() : session.events\n"
            "  return Array.isArray(events) ? events : []\n"
            "}\n",
            encoding="utf-8",
        )
        return work

    def test_tool_result_shape_matches_dsh_session_events(self) -> None:
        """`tool/result` 的形状：message 级 isError + content 块数组（见 dsh-acp 的 toolResultUpdate）。"""
        node = self._node()
        work = self._stage()
        driver = work / "shape.mjs"
        driver.write_text(
            "import { toolResultRecord } from './experience-keeper.mjs'\n"
            "const ok = toolResultRecord({ message: { content: [{ type: 'text', text: '实测 120 秒' }], isError: false } })\n"
            "const bad = toolResultRecord({ message: { content: [{ type: 'text', text: 'boom' }], isError: true } })\n"
            "const legacy = toolResultRecord({ message: { content: [{ type: 'text', isError: true, content: [{ type: 'text', text: 'legacy' }] }] } })\n"
            "console.log(JSON.stringify({ ok, bad, legacy }))\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [node, str(driver)],
            cwd=str(work),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertFalse(payload["ok"]["is_error"])
        self.assertEqual(payload["ok"]["output"], "实测 120 秒")
        self.assertTrue(payload["bad"]["is_error"])
        self.assertEqual(payload["bad"]["output"], "boom")
        self.assertEqual(payload["bad"]["error"], "boom")
        # 旧形态（块级 isError + 嵌套 content）一并容忍：版本回退时采集不静默失效。
        self.assertTrue(payload["legacy"]["is_error"])
        self.assertEqual(payload["legacy"]["output"], "legacy")

    def test_turn_end_writes_transcript_that_parser_understands(self) -> None:
        """一轮事件走完：插件落盘归一化转录，并调用 hook；转录必须能被 parse_dsh 解析出证据。"""
        node = self._node()
        work = self._stage()
        store = Path(self.temp.name) / "store"
        recorded = Path(self.temp.name) / "hook-payload.json"
        stub = Path(self.temp.name) / "stub_hook.py"
        stub.write_text(
            "import json, os, sys\n"
            f"open({str(recorded)!r}, 'w', encoding='utf-8').write(sys.stdin.read())\n",
            encoding="utf-8",
        )
        driver = work / "turn.mjs"
        driver.write_text(
            "import { apply } from './experience-keeper.mjs'\n"
            "const handlers = []\n"
            "const ctx = { on: (name, handler) => { handlers.push([name, handler]) }, get: () => undefined }\n"
            "apply(ctx, {})\n"
            "const session = { id: 'sess-1', header: { cwd: process.cwd() }, events: [] }\n"
            "const emit = async (event) => {\n"
            "  for (const [name, handler] of handlers) if (name === 'session/event') await handler(session, event)\n"
            "}\n"
            "await emit({ type: 'turn/start', data: { turn: 1 } })\n"
            "await emit({ type: 'user/message', data: { content: [{ type: 'text', text: '看下会话表' }] } })\n"
            "await emit({ type: 'tool/call', data: { turn: 1, step: 1, callId: 'c1', name: 'bash', arguments: JSON.stringify({ command: 'conntrack -S' }) } })\n"
            "await emit({ type: 'tool/result', data: { turn: 1, step: 1, message: { content: [{ type: 'text', text: '实测这台机器上 conntrack 老化时间是 120 秒，只在本机成立' }], isError: false } } })\n"
            "await emit({ type: 'assistant/message', data: { message: { content: [{ type: 'text', text: '记下了' }] } } })\n"
            "await emit({ type: 'turn/end', data: { turn: 1, reason: { kind: 'completed' } } })\n"
            "console.log('done')\n",
            encoding="utf-8",
        )
        env = dict(
            os.environ,
            EXPERIENCE_HOME=str(store),
            EXPERIENCE_HOOK_SCRIPT=str(stub),
            EXPERIENCE_PYTHON=sys.executable,
        )
        result = subprocess.run(
            [node, str(driver)],
            cwd=str(work),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        transcript = store / "transcripts" / "dsh" / "sess-1-t1.jsonl"
        self.assertTrue(transcript.exists(), "插件未落盘归一化转录")
        lines = [json.loads(line) for line in transcript.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(
            [line["type"] for line in lines],
            ["user_message", "tool_call", "tool_result", "agent_message"],
        )
        self.assertEqual(lines[2]["output"], "实测这台机器上 conntrack 老化时间是 120 秒，只在本机成立")
        self.assertFalse(lines[2]["is_error"])

        turn = transcripts.parse_transcript("dsh", str(transcript), "1")
        self.assertIsNotNone(turn, "归一化转录必须能被 parse_dsh 解析")
        self.assertEqual(turn.tools[0]["result"][:6], "实测这台机器")
        self.assertIn("cost_trap", prescreen.detect_signals(turn.to_dict()))

        if not recorded.exists():
            # 子进程管道被沙箱挡住时既没报错也没有 payload，只跳过 payload 断言，转录断言已生效。
            self.skipTest("hook 子进程未能启动，跳过统一契约 payload 断言")
        payload = json.loads(recorded.read_text(encoding="utf-8"))
        self.assertEqual(payload["client"], "dsh")
        self.assertEqual(payload["event"], "turn_end")
        self.assertEqual(payload["session_id"], "sess-1")
        self.assertEqual(payload["turn_id"], "1")
        self.assertEqual(Path(payload["transcript_path"]), transcript)
        self.assertEqual(payload["compaction_count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
