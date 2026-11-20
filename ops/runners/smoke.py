import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from fastapi.testclient import TestClient

from src.api.main import create_app
from src.core.config import Settings


def main() -> None:
    start = perf_counter()
    with TemporaryDirectory() as tmp:
        settings = Settings(
            database_path=Path(tmp) / "smoke.db", llm_mode="mock", bitrix_mode="mock",
            bitrix_approver_tokens={},
        )
        with TestClient(create_app(settings)) as client:
            client.get("/health").raise_for_status()
            scan = client.post("/api/v1/reviver/scan", json={}, headers={"Idempotency-Key": "smoke"})
            scan.raise_for_status()
            items = scan.json()["decisions"]
            candidate = next(item for item in items if item["should_revive"])
            approval = client.post(
                f"/api/v1/reviver/approve/{candidate['revival_id']}", json={"approve": True},
            )
            approval.raise_for_status()
            assert approval.json()["bitrix_sync_status"] == "synced"
            metrics = client.get("/api/v1/metrics")
            metrics.raise_for_status()
            print(json.dumps({"deals": len(items), "elapsed_sec": round(perf_counter() - start, 3),
                              **metrics.json()}, indent=2))


if __name__ == "__main__":
    main()
