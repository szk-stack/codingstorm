"""应用入口。"""

from __future__ import annotations

import argparse
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from codingstorm.api import router
from codingstorm.config import Config
from codingstorm.db import Database
from codingstorm.runner import Runner
from codingstorm.scheduler import Scheduler
from codingstorm.store import Store
from codingstorm.workspace import WorkspaceManager

log = logging.getLogger("codingstorm")


def create_app(config: Config, *, start_scheduler: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config.ensure_dirs()
        db = Database(config.db_path)
        db.start()
        store = Store(db)
        runner = Runner(config, store)
        scheduler = Scheduler(config, store, runner, WorkspaceManager(config))

        app.state.config = config
        app.state.db = db
        app.state.store = store
        app.state.runner = runner
        app.state.scheduler = scheduler

        if start_scheduler:
            # 里面会先做崩溃对账：杀掉遗留的孤儿进程、把中断任务标出来
            await scheduler.start()
        log.info("codingstorm 已启动，数据目录 %s", config.root)
        try:
            yield
        finally:
            # 顺序不能反：先让调度器写库并终止子进程，再关数据层。
            # 信号处理交给 uvicorn 的 lifespan，不要用 loop.add_signal_handler 抢。
            if start_scheduler:
                await scheduler.stop()
            db.close()
            log.info("codingstorm 已停止")

    app = FastAPI(title="codingstorm", lifespan=lifespan)
    app.state.config = config
    app.include_router(router)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(prog="codingstorm", description="按项目隔离的 Claude Code 任务队列")
    parser.add_argument("--config", type=Path, default=None, help="TOML 配置文件路径")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument(
        "--no-scheduler", action="store_true", help="只起 HTTP 服务，不执行任务（调试用）"
    )
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = Config.load(args.config)
    app = create_app(config, start_scheduler=not args.no_scheduler)
    uvicorn.run(
        app,
        host=args.host or config.server.host,
        port=args.port or config.server.port,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
