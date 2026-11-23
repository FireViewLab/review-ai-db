"""P_text v4.0 단독 ASGI 진입점. v1(MySQL)·v2(파일 작업)를 읽지 않고 v4 모델만 올린다."""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v4_analyze import router as v4_router
from app.core.internal_auth import load_internal_token


def create_v4_app(predictor=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        app.state.internal_token = load_internal_token()
        if app.state.internal_token is None:
            raise ValueError("The v4 service requires INTERNAL_TOKEN")
        if predictor is not None:
            app.state.v4_predictor = predictor
        else:
            from app.analyzers.p_text_v4 import V4Predictor

            path = os.getenv("PTEXT_V4_MODEL_PATH", "")
            if not path:
                raise ValueError("The v4 service requires PTEXT_V4_MODEL_PATH")
            batch = int(os.getenv("PTEXT_V4_BATCH_SIZE", "16"))
            app.state.v4_predictor = V4Predictor.from_model_dir(path, batch_size=batch)
        yield

    app = FastAPI(title="Re:view AI v4", lifespan=lifespan)
    app.include_router(v4_router)
    return app


app = create_v4_app()
