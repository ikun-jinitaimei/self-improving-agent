"""可选择的真实 Docker 集成验收，不请求 LLM。

默认跳过，不能把 skipped 称为容器通过。先准备 Docker Desktop/Linux containers
和 python:3.12-slim，再设置 AGENT_LAB_TEST_DOCKER=1 执行本文件。
一旦显式启用，缺 CLI/daemon/镜像会使测试失败，而不是悄悄跳过。
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from execution import check_docker
from tools import execute_tool


@unittest.skipUnless(os.environ.get("AGENT_LAB_TEST_DOCKER") == "1", "Docker integration not requested")
class ContainerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        check_docker()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.data.mkdir()
        (self.data / "input.csv").write_text("x\n1\n", encoding="utf-8")
        (self.root / "reference.private.json").write_text('{"answer":999}', encoding="utf-8")

    def run_code(self, code, timeout=10):
        """走与实际模型一致的工具路径，测试代码为本文件的明确固定代码。"""
        return execute_tool("run_python", {"code": code, "timeout_seconds": timeout},
                            data_root=self.data, backend="docker")

    def test_container_can_read_only_supplied_csv(self):
        result = self.run_code("import csv; print(len(list(csv.DictReader(open('input.csv')))))")
        self.assertEqual(result["status"], "success")
        self.assertIn("STDOUT:\n1", result["observation"])

    def test_container_cannot_write_data_or_root(self):
        for path in ("/data/changed.csv", "/changed.txt"):
            with self.subTest(path=path):
                result = self.run_code(f"open({path!r}, 'w').write('changed')")
                self.assertEqual(result["status"], "execution_error")
        self.assertFalse((self.data / "changed.csv").exists())

    def test_container_has_no_reference_or_api_key(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fake-integration-key"}):
            result = self.run_code("import os; from pathlib import Path; "
                "assert not Path('/reference.private.json').exists(); "
                "assert not Path('/data/../reference.private.json').exists(); "
                "assert 'DEEPSEEK_API_KEY' not in os.environ; print('isolated')")
        self.assertEqual(result["status"], "success")
        self.assertIn("isolated", result["observation"])

    def test_container_runs_nonroot_with_scratch_directory(self):
        result = self.run_code("import os; assert os.getuid() != 0; "
                               "open('/tmp/check.txt', 'w').write('ok'); print('nonroot')")
        self.assertEqual(result["status"], "success")

    def test_container_external_network_is_rejected(self):
        # 直接 IP 避开 DNS 差异。禁止网络时连接必须失败，超时也视为拒绝。
        result = self.run_code("import socket; socket.create_connection(('1.1.1.1', 443), timeout=2)")
        self.assertEqual(result["status"], "execution_error")

    def test_container_actual_cgroup_resource_limits(self):
        """读取真实 cgroup v2 上限，不只检查命令里有没有 --memory 字样。

        不做耗尽内存/大量 fork 的压力测试；只核实 Engine 实际应用了资源配置。
        本验收目标是当前 Linux cgroup v2，其他宿主配置仍需另行测试。
        """
        result = self.run_code(
            "from pathlib import Path; root=Path('/sys/fs/cgroup'); "
            "assert int((root/'memory.max').read_text()) == 256*1024*1024; "
            "assert int((root/'pids.max').read_text()) == 64; "
            "quota,period=map(int,(root/'cpu.max').read_text().split()); "
            "assert quota == period; print('resource limits applied')")
        self.assertEqual(result["status"], "success")
        self.assertIn("resource limits applied", result["observation"])

    def test_timed_out_container_is_removed(self):
        # 工具 timeout 后只清理本次随机名称的容器；不能清理其他用户容器。
        before = subprocess.run(["docker", "ps", "-aq", "--filter", "name=sia-"],
                                capture_output=True, text=True, timeout=10, check=True).stdout.splitlines()
        result = self.run_code("import time; time.sleep(20)", timeout=2)
        self.assertEqual(result["status"], "execution_error")
        after = subprocess.run(["docker", "ps", "-aq", "--filter", "name=sia-"],
                               capture_output=True, text=True, timeout=10, check=True).stdout.splitlines()
        self.assertFalse(set(after) - set(before), "本次超时遗留了容器")


if __name__ == "__main__":
    unittest.main()
