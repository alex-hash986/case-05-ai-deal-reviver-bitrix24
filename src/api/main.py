from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request

from src.core.config import ROOT, Settings
from src.core.database import Database
from src.core.llm_gateway import LLMGateway
from src.domain.ports import BitrixPort
from src.domain.schemas import ApprovalIn, Metrics, RevivalDecisionOut, ScanIn, ScanOut
from src.domain.service import Conflict, Reviver
from src.integrations.bitrix_connector import MockBitrix
from src.integrations.bitrix_live import BitrixLiveAdapter


def get_service(request: Request) -> Reviver:
    service: Reviver = request.app.state.reviver
    return service


Service = Annotated[Reviver, Depends(get_service)]


def create_app(
    settings: Settings | None = None, *, bitrix: BitrixPort | None = None,
    llm_transport: httpx.AsyncBaseTransport | None = None,
    bitrix_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        config = settings or Settings()
        path = ROOT / config.database_path
        db = Database(path)
        async with (
            httpx.AsyncClient(timeout=30, transport=llm_transport) as client,
            httpx.AsyncClient(timeout=30, transport=bitrix_transport) as crm_client,
        ):
            connector = bitrix
            if connector is None:
                if config.bitrix_mode == "live":
                    connector = BitrixLiveAdapter(config, db, crm_client)
                    db.bind_live_portal(httpx.URL(connector.base_url).netloc.decode())
                else:
                    db.require_mock()
                    connector = MockBitrix(db, ROOT / "data/demo_deals.json", datetime.now(UTC))
            app.state.reviver = Reviver(
                config, db, connector, LLMGateway(config, client),
                current_assignee=connector.current_assignee if isinstance(connector, BitrixLiveAdapter) else None,
            )
            yield

    app = FastAPI(title="AI Deal Reviver · Case 05", lifespan=lifespan)

    @app.get("/health")
    def health(service: Service) -> dict[str, str]:
        with service.db.connect() as connection:
            mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
        if mode != "wal":
            raise HTTPException(503, "SQLite WAL unavailable")
        return {"status": "ok", "database": mode}

    @app.post("/api/v1/reviver/scan")
    async def scan(
        request: ScanIn, service: Service,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=128, pattern=r"^\S+$")],
    ) -> ScanOut:
        try:
            return await service.scan(idempotency_key, request)
        except Conflict as error:
            raise HTTPException(409, str(error)) from error
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise HTTPException(502, "Bitrix scan failed") from error

    @app.get("/api/v1/reviver/queue")
    def queue(service: Service) -> list[RevivalDecisionOut]:
        return service.queue()

    @app.post("/api/v1/reviver/approve/{revival_id}")
    async def approve(
        revival_id: str, request: ApprovalIn, service: Service,
        x_approval_token: Annotated[str, Header(max_length=512)] = "",
    ) -> RevivalDecisionOut:
        try:
            result = await service.approve(revival_id, request.approve, request.approver_user_id, x_approval_token)
        except PermissionError as error:
            raise HTTPException(403, str(error)) from error
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise HTTPException(502, "Bitrix approval verification failed") from error
        except Conflict as error:
            raise HTTPException(409, str(error)) from error
        if result is None:
            raise HTTPException(404, "Revival item not found")
        return result

    @app.post("/api/v1/reviver/reconcile")
    async def reconcile(service: Service) -> dict[str, int]:
        return {"completed_steps": await service.reconcile()}

    @app.get("/api/v1/metrics")
    def metrics(service: Service) -> Metrics:
        return service.metrics()

    return app


app = create_app()
