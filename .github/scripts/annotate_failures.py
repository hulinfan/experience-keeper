"""把 pytest 失败摘要写成 GitHub 注解。

公开仓的 job 页对未登录用户只渲染注解与步骤状态，不渲染日志正文；失败时把关键行
打成 `::error::`，定位问题不需要额外凭据。
"""

import pathlib
import sys


def escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")


def main() -> int:
    path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "pytest.log")
    if not path.exists():
        print("::error title=pytest::pytest.log 不存在，测试可能没跑起来")
        return 0
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    failed = [line for line in lines if line.startswith(("FAILED", "ERROR"))]
    print(f"::error title=pytest 失败用例数::{len(failed)}")
    for index, line in enumerate(failed[:8]):
        print(f"::error title=failed-{index + 1}::{escape(line)}")
    tail = "\n".join(lines[-60:])
    print(f"::error title=pytest 日志尾部::{escape(tail)[-6000:]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
