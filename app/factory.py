"""결과 저장소와 선택적 크롤러 SSE 연결의 수명을 관리한다."""

import os
import asyncio
from contextlib import asynccontextmanager
from app.repositories.mysql_jobs import MySQLJobStore

from dotenv import load_dotenv

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.api.routes import router
from app.api.data_analysis import router as data_router
from app.api.data_analysis_stream import router as data_stream_router
from app.core.internal_auth import load_internal_token


def create_app(*, job_store=None, v2_job_store=None, v2_groq=None) -> FastAPI:
    load_dotenv()
    experimental = os.getenv("ENABLE_EXPERIMENTAL_COLLECTION", "0") == "1"
    v2 = os.getenv("ENABLE_DATA_AI_V2", "0") == "1"

    @asynccontextmanager
    async def lifespan(app):
        app.state.internal_token = load_internal_token()
        store = job_store if job_store is not None else MySQLJobStore.from_env()
        store.initialize()
        app.state.job_store = store
        app.state.analysis_tasks = set()
        if v2:
            from app.integrations.groq_v2_summary import V2GroqSummary

            # Explicit migration creates the additive v2 tables. Missing v2 tables
            # fail v2 claims, not v1 startup or /health.
            app.state.v2_job_store = v2_job_store if v2_job_store is not None else MySQLJobStore.from_env(scope="v2")
            app.state.v2_groq = v2_groq if v2_groq is not None else V2GroqSummary.from_env()
        crawler = None
        if experimental:
            from app.integrations.crawler_stream import CrawlerStreamClient
            crawler = CrawlerStreamClient()
            app.state.crawler_stream = crawler
        try:
            yield
        finally:
            if app.state.analysis_tasks:
                await asyncio.gather(*app.state.analysis_tasks, return_exceptions=True)
            if crawler is not None:
                await crawler.aclose()

    application = FastAPI(
        title="Re:view AI Analysis Service",
        version="1.0.0",
        lifespan=lifespan,
    )
    application.include_router(router)
    application.include_router(data_router)
    application.include_router(data_stream_router)
    if v2:
        from app.api.data_analysis_v2 import router as v2_router

        application.include_router(v2_router)
    if experimental:
        from app.api.collection import router as collection_router
        application.include_router(collection_router)

    @application.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse(url="/docs")

    return application
