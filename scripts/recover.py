"""核对当前 data_dir 的旧执行器归属；不删源项目或个人 Harness 数据。"""

import argparse
import asyncio
from pathlib import Path
import sys

from harness_mcp.config import load_config
from harness_mcp.task_manager import TaskManager


async def recover(path: Path) -> bool:
    manager = TaskManager(load_config(path.resolve()))
    try:
        stopped = await manager._recover_owned()
        manager.recovery_confirmed = stopped
        manager.store.recover_interrupted(stopped)
        return stopped
    finally:
        await manager.close()


def main() -> None:
    if hasattr(sys.stdout,"reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",type=Path,required=True)
    args = parser.parse_args()
    success = asyncio.run(recover(args.config))
    print("旧执行器已确认停止；任务数据及源项目均保留。" if success else "停止尚未确认，任务保留中断待核对状态；请检查所配置的执行器环境。")
    raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    main()
