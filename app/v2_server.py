"""Standalone v2 ASGI entry point; does not import or initialize a MySQL store."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.data_analysis_v2 import router as v2_router
from app.api.routes import router as health_router
from app.core.internal_auth import load_internal_token
from app.integrations.groq_v2_summary import V2GroqSummary
from app.repositories.v2_file_jobs import FileV2JobStore


def create_v2_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        app.state.internal_token = load_internal_token()
        if app.state.internal_token is None:
            raise ValueError("The standalone v2 service requires INTERNAL_TOKEN")
        app.state.v2_job_store = FileV2JobStore.from_env()
        app.state.v2_groq = V2GroqSummary.from_env()
        app.state.analysis_tasks = set()
        try:
            yield
        finally:
            if app.state.analysis_tasks:
                await asyncio.gather(*app.state.analysis_tasks, return_exceptions=True)

    app = FastAPI(title="Re:view AI v2 SSE", lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(v2_router)
    return app


app = create_v2_app()
