from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient


PORTAL_ROOT = Path(__file__).resolve().parents[2]
TEMP_ROOT = Path(tempfile.mkdtemp(prefix="cindy-portal-contract-"))
VOCAB_ACCESS_KEY = "vocab-contract-access-key-24"
os.environ["TREND_DESK_PORTAL_DIR"] = str(PORTAL_ROOT)
os.environ["TREND_DESK_DB_PATH"] = str(TEMP_ROOT / "trend-desk.db")
os.environ["TREND_DESK_DATA_DIR"] = str(TEMP_ROOT / "data")
os.environ["TREND_DAILY_SCHEDULER_ENABLED"] = "false"
os.environ["AI_BUILDER_TOKEN"] = "contract-provider-token"
os.environ["SENSORY_VOCAB_ACCESS_KEY_SHA256"] = hashlib.sha256(
    VOCAB_ACCESS_KEY.encode()
).hexdigest()

from backend import config, schema  # noqa: E402
from backend.portal_app import app  # noqa: E402


def test_portal_schema_contract_matches_production_migration():
    assert schema.ALEMBIC_HEAD == "20260818_01"


def test_portal_and_legacy_projects_are_served():
    with TestClient(app) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert "张小由的空间" in home.text
        for path in (
            "/bracelet/",
            "/neican/",
            "/research/",
            "/bridge-blocker/",
            "/health/",
            "/kids/",
            "/sensory-vocabulary-lab/",
        ):
            response = client.get(path)
            assert response.status_code == 200, path
        assert 'href="sensory-vocabulary-lab/"' in home.text


def test_manifest_and_mounted_trend_desk_contract():
    expected = json.loads(
        (PORTAL_ROOT / "deployment-manifest.json").read_text(encoding="utf-8")
    )
    with TestClient(app) as client:
        manifest = client.get("/deployment-manifest.json")
        assert manifest.status_code == 200
        assert manifest.json() == expected

        health = client.get("/trend-desk/api/health")
        assert health.status_code == 200
        assert health.json() == {"ok": True}
        assert client.get("/trend-desk/api/batches").status_code == 401


def test_sensory_vocabulary_lab_mount_and_ai_builder_models():
    with TestClient(app, base_url="https://testserver") as client:
        home = client.get("/sensory-vocabulary-lab/")
        assert home.status_code == 200
        assert "词汇感官实验室" in home.text
        assert client.get("/sensory-vocabulary-lab/app.js").status_code == 200
        assert client.get("/sensory-vocabulary-lab/styles.css").status_code == 200
        assert (
            client.get(
                "/sensory-vocabulary-lab/assets/illustrations/pour.png"
            ).status_code
            == 200
        )

        health = client.get("/sensory-vocabulary-lab/api/health")
        assert health.status_code == 200
        assert health.json() == {
            "status": "ok",
            "mode": "live",
            "provider": "ai_builder",
            "credentialSource": "AI_BUILDER_TOKEN",
            "textModel": "gpt-5",
            "imageModel": "gpt-image-1.5",
            "styleVersion": "sensory-ink-v1",
            "persistentLibrary": True,
            "libraryReady": True,
            "accessConfigured": True,
        }

        assert client.get("/sensory-vocabulary-lab/api/demo").status_code == 401
        assert client.post(
            "/sensory-vocabulary-lab/api/access",
            json={"key": VOCAB_ACCESS_KEY},
        ).status_code == 200
        demo = client.get("/sensory-vocabulary-lab/api/demo")
        assert demo.status_code == 200
        assert [item["word"] for item in demo.json()["words"]] == [
            "pour",
            "individual",
            "transfer",
        ]

        bad_plan = client.post("/sensory-vocabulary-lab/api/plan", json={})
        assert bad_plan.status_code == 400
        assert bad_plan.json()["error"]["code"] == "invalid_words"


def test_mounted_login_and_automation_key_contract(monkeypatch):
    access_key = "portal-contract-access-key"
    monkeypatch.setenv(
        "TREND_DESK_ACCESS_KEY_SHA256",
        hashlib.sha256(access_key.encode()).hexdigest(),
    )
    monkeypatch.setattr(config, "AUTOMATION_SECRET", "automation-secret")
    monkeypatch.setattr(config, "AUTOMATION_ENABLED", False)

    with TestClient(app, base_url="https://testserver") as client:
        assert client.post(
            "/trend-desk/api/auth/login",
            json={"access_key": access_key},
        ).status_code == 200
        assert client.get("/trend-desk/api/batches").status_code == 200

        assert client.post(
            "/trend-desk/api/automation/tick",
            json={"stage": "finalize"},
        ).status_code == 401
        response = client.post(
            "/trend-desk/api/automation/tick",
            headers={"x-automation-key": "automation-secret"},
            json={"stage": "finalize", "trade_date": "2099-12-31"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "skipped"
