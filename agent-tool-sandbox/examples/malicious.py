"""安全测试：向沙箱提交恶意代码，验证它全部被拦下（设计说明书 §29-32）。

这就是「一定不要只测试正常代码」—— 每个恶意样本都必须被沙箱终结。

先启动服务：
    uvicorn app.main:app --port 8000

再运行：
    python examples/malicious.py
"""
from __future__ import annotations

import sys
import time

import httpx

from python import submit

# 恶意样本清单：code → 预期终态
MALICIOUS = [
    # Test 1：无限循环 → TIMEOUT
    ("while True:\n    pass\n", "timeout"),
    # Test 2：无限内存 → OOM
    ("x = []\nwhile True:\n    x.append('A' * 1024 * 1024)\n", "oom"),
    # Test 3：读取宿主文件 → REJECTED
    ('open("/host-secret")', "rejected"),
    # Test 4：访问外网 → REJECTED
    ("import requests\nrequests.get('https://example.com')", "rejected"),
    # Test 5：访问私网 / metadata → REJECTED（allow-list 拒绝）
    ('import socket\nsocket.socket()', "rejected"),
    # Test 6：fork bomb → FAILED（PID limit）/ 静态 warning
    ("import subprocess\nwhile True:\n    subprocess.Popen(['sleep', '30'])\n", "failed"),
    # Test 7：磁盘爆炸 → 被 /tmp 或磁盘上限拦下
    ('while True:\n    open("/workspace/output/blob", "ab").write(b"A" * 65536)\n', "failed"),
    # Test 8：输出爆炸 → OUTPUT_LIMIT_EXCEEDED
    ("print('A' * 1024 * 1024)", "output_limit_exceeded"),
]

EXPECTED_ALIASES = {
    "timeout": {"timeout"},
    "oom": {"oom", "timeout"},            # 极慢机器上可能先超时
    "rejected": {"rejected"},
    # PID/磁盘炸弹：可能被 PID limit、内存、超时任一机制终止，反正不能一直跑
    "failed": {"failed", "timeout", "oom", "killed"},
    "output_limit_exceeded": {"output_limit_exceeded", "timeout"},
}


def main() -> int:
    failed = 0
    for i, (code, expected) in enumerate(MALICIOUS, 1):
        view = submit("python", code)
        ok = view["status"] in EXPECTED_ALIASES[expected]
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"[{mark}] Test{i:>2} 期望~{expected:<22} 实际={view['status']}"
              f"  ({view['duration_ms']}ms)")
    print(f"\n{len(MALICIOUS) - failed}/{len(MALICIOUS)} 个恶意样本被成功拦下")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
