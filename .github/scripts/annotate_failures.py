"""把 pytest 失败摘要写成 GitHub 注解。

公开仓的 job 页对未登录用户只渲染注解与步骤状态，不渲染日志正文；失败时把关键行
打成 `::error::`，定位问题不需要额外凭据。

注解读端有长度上限，超长会整条丢弃：每行截断，总长度受控。
"""

import pathlib
import sys

MAX_LINES = 12
MAX_LINE_CHARS = 240
MAX_TOTAL_CHARS = 4000


def escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")


def trim(line: str) -> str:
    line = line.strip()
    return line if len(line) <= MAX_LINE_CHARS else line[: MAX_LINE_CHARS - 1] + "…"


def main() -> int:
    path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "pytest.log")
    if not path.exists():
        print("::error title=pytest::pytest.log 不存在，测试可能没跑起来")
        return 0
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    failed = [trim(line) for line in lines if line.startswith(("FAILED", "ERROR"))]
    print(f"::error title=pytest 失败用例数::{len(failed)}")
    for index, line in enumerate(failed[:MAX_LINES]):
        print(f"::error title=failed-{index + 1}::{escape(line)}")
    tail = [trim(line) for line in lines[-MAX_LINES:]]
    print(f"::error title=pytest 日志尾部::{escape(chr(10).join(tail))[-MAX_TOTAL_CHARS:]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
