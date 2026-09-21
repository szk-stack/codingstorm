"""应用入口。"""

from __future__ import annotations

import argparse
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from codingstorm.api import STATIC_DIR, pages, router
from codingstorm.config import Config
from codingstorm.context import ContextStore
from codingstorm.db import Database
from codingstorm.events import EventBus
from codingstorm.guard import GuardInstaller
from codingstorm.pricing import PriceTable
from codingstorm.runner import Runner
from codingstorm.scheduler import Scheduler
from codingstorm.store import Store
from codingstorm.workspace import WorkspaceManager

log = logging.getLogger("codingstorm")


def create_app(config: Config, *, start_scheduler: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config.ensure_dirs()

        # 装配边界拦截钩子。`--settings` 是**合并**进用户配置的（Phase 0 附带实测过），
        # 所以凭证等设置不受影响。
        guard_settings = GuardInstaller(config).install()
        if guard_settings is not None:
            config.claude.settings_file = guard_settings

        prices = PriceTable.load(
            config.pricing.prices_file or (config.root / "prices.toml")
        )
        if not prices.configured:
            log.info("未配置价目表（%s），成本将留空", config.root / "prices.toml")

        db = Database(config.db_path)
        db.start()
        store = Store(db)
        bus = EventBus()

        async def on_event(event: dict) -> None:
            task_id = event.pop("task_id", None)
            if task_id:
                bus.publish(task_id, event)

        runner = Runner(config, store, on_event=on_event)
        workspaces = WorkspaceManager(config)
        contexts = ContextStore(config)
        scheduler = Scheduler(config, store, runner, workspaces, contexts, prices)

        app.state.config = config
        app.state.db = db
        app.state.store = store
        app.state.bus = bus
        app.state.runner = runner
        app.state.workspaces = workspaces
        app.state.contexts = contexts
        app.state.prices = prices
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

    @app.middleware("http")
    async def revalidate_assets(request, call_next):
        """页面外壳一律要求浏览器回源校验。

        不设的话浏览器会把 app.js 缓存住不再来取 —— 部署完新版本，页面上跑的还是旧的，
        而 index.html 是新的，两边对不上：实测新加的「文件」页签点上去毫无反应，
        看着像功能坏了，其实是 JS 没更新。`no-cache` 是「用之前先问一声」，
        文件没变时靠 ETag 走 304，不额外传内容。
        """
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["cache-control"] = "no-cache"
        return response

    app.state.config = config
    app.include_router(router)
    app.include_router(pages)
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
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
