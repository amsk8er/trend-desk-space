from __future__ import annotations

import hashlib
import io
import os

from fastapi.testclient import TestClient
from PIL import Image


ACCESS_KEY = "vocab-contract-access-key-24"
os.environ["AI_BUILDER_TOKEN"] = "library-test-provider-token"
os.environ["SENSORY_VOCAB_ACCESS_KEY_SHA256"] = hashlib.sha256(
    ACCESS_KEY.encode()
).hexdigest()

from backend.portal_app import app  # noqa: E402
from backend import sensory_vocab_app as vocab_app  # noqa: E402
from backend import sensory_vocab_store as vocab_store  # noqa: E402


def login(client: TestClient) -> None:
    response = client.post(
        "/sensory-vocabulary-lab/api/access",
        json={"key": ACCESS_KEY},
    )
    assert response.status_code == 200


def word_payload(word: str, core: str | None = None) -> dict:
    return {
        "word": word,
        "pos": "verb",
        "phonetic": "/test/",
        "coreSenseCn": core or f"{word} 的核心",
        "sensoryPromptCn": "身体先感觉到了什么？",
        "feelingChips": ["方向", "力量", "变化"],
        "exampleEn": f"We {word} it.",
        "exampleZh": "这是一个例句。",
        "scenes": [
            {
                "captionCn": "场景一",
                "imagePrompt": f"A direct physical scene for {word}, first.",
                "usageHookCn": "第一种语境。",
            },
            {
                "captionCn": "场景二",
                "imagePrompt": f"A direct physical scene for {word}, second.",
                "usageHookCn": "第二种语境。",
            },
        ],
    }


def fake_plan(calls: list[tuple[tuple[str, ...], str]]):
    def planner(words: list[str], level: str, _key: str) -> dict:
        calls.append((tuple(words), level))
        return {
            "id": "upstream-pack",
            "title": ", ".join(words),
            "level": level,
            "date": "2026/07/30",
            "source": "live",
            "words": [word_payload(word) for word in words],
        }

    return planner


def png_bytes(color: str) -> bytes:
    image = Image.new("RGB", (320, 220), color)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_fixed_key_gate_and_logout():
    with TestClient(app, base_url="https://testserver") as client:
        status = client.get("/sensory-vocabulary-lab/api/access")
        assert status.status_code == 200
        assert status.json() == {"configured": True, "authenticated": False}
        assert client.get("/sensory-vocabulary-lab/api/packs").status_code == 401
        assert client.post(
            "/sensory-vocabulary-lab/api/access",
            json={"key": ACCESS_KEY},
            headers={"Origin": "https://elsewhere.example"},
        ).status_code == 403
        assert client.post(
            "/sensory-vocabulary-lab/api/access",
            json={"key": "wrong-key"},
        ).status_code == 401
        response = client.post(
            "/sensory-vocabulary-lab/api/access",
            json={"key": ACCESS_KEY},
            headers={"Origin": "https://testserver"},
        )
        assert response.status_code == 200
        assert client.get("/sensory-vocabulary-lab/api/packs").status_code == 200
        assert client.delete("/sensory-vocabulary-lab/api/access").status_code == 200
        assert client.get("/sensory-vocabulary-lab/api/packs").status_code == 401


def test_fixed_key_gate_accepts_https_origin_behind_proxy():
    with TestClient(app, base_url="http://internal-service") as client:
        response = client.post(
            "/sensory-vocabulary-lab/api/access",
            json={"key": ACCESS_KEY},
            headers={
                "Host": "vocab.example",
                "Origin": "https://vocab.example",
                "X-Forwarded-Proto": "https",
            },
        )
        assert response.status_code == 200


def test_generation_lease_has_single_owner():
    word = "leasecacheprobe"
    first_owned, first_waiting = vocab_store.claim_generation_leases(
        [word],
        "PET",
        "owner-one",
    )
    second_owned, second_waiting = vocab_store.claim_generation_leases(
        [word],
        "PET",
        "owner-two",
    )
    assert first_owned == [word]
    assert first_waiting == []
    assert second_owned == []
    assert second_waiting == [word]
    vocab_store.release_generation_leases("owner-one")
    retry_owned, retry_waiting = vocab_store.claim_generation_leases(
        [word],
        "PET",
        "owner-two",
    )
    assert retry_owned == [word]
    assert retry_waiting == []
    vocab_store.release_generation_leases("owner-two")


def test_same_word_and_level_reuses_text_and_image(monkeypatch):
    calls: list[tuple[tuple[str, ...], str]] = []
    monkeypatch.setattr(vocab_app.vocab, "plan_wordpack", fake_plan(calls))
    monkeypatch.setattr(
        vocab_app,
        "generated_image_bytes",
        lambda **_kwargs: (png_bytes("#2f80b8"), {"cached": False}),
    )
    word = "cacheprobealpha"

    with TestClient(app, base_url="https://testserver") as client:
        login(client)
        first = client.post(
            "/sensory-vocabulary-lab/api/packs",
            json={"words": [word], "level": "PET"},
        )
        assert first.status_code == 200, first.text
        first_pack = first.json()["pack"]
        scene_id = first_pack["words"][0]["scenes"][0]["id"]
        image = client.post(
            "/sensory-vocabulary-lab/api/generate-image",
            json={"sceneId": scene_id},
        )
        assert image.status_code == 200, image.text
        image_url = image.json()["image"]

        second = client.post(
            "/sensory-vocabulary-lab/api/packs",
            json={"words": [word.upper()], "level": "PET"},
        )
        assert second.status_code == 200, second.text
        assert second.json()["cacheHits"] == [word.lower()]
        assert second.json()["generatedWords"] == []
        assert second.json()["pack"]["words"][0]["scenes"][0]["image"] == image_url
        assert calls == [((word,), "PET")]

        different_level = client.post(
            "/sensory-vocabulary-lab/api/packs",
            json={"words": [word], "level": "GRE"},
        )
        assert different_level.status_code == 200
        assert calls[-1] == ((word,), "GRE")


def test_scene_regeneration_updates_every_referencing_pack(monkeypatch):
    calls: list[tuple[tuple[str, ...], str]] = []
    monkeypatch.setattr(vocab_app.vocab, "plan_wordpack", fake_plan(calls))
    colors = iter(["#247f54", "#d8792b"])
    monkeypatch.setattr(
        vocab_app,
        "generated_image_bytes",
        lambda **_kwargs: (png_bytes(next(colors)), {"cached": False}),
    )
    word = "cacheprobebeta"

    with TestClient(app, base_url="https://testserver") as client:
        login(client)
        first = client.post(
            "/sensory-vocabulary-lab/api/packs",
            json={"words": [word], "level": "FCE"},
        ).json()["pack"]
        second = client.post(
            "/sensory-vocabulary-lab/api/packs",
            json={"words": [word], "level": "FCE"},
        ).json()["pack"]
        scene_id = first["words"][0]["scenes"][0]["id"]
        initial = client.post(
            "/sensory-vocabulary-lab/api/generate-image",
            json={"sceneId": scene_id},
        ).json()["image"]
        regenerated = client.post(
            f"/sensory-vocabulary-lab/api/scenes/{scene_id}/regenerate",
            json={},
        )
        assert regenerated.status_code == 200, regenerated.text
        replacement = regenerated.json()["image"]
        assert replacement != initial
        assert regenerated.json()["affectedPacks"] == 2

        for pack_id in (first["id"], second["id"]):
            pack = client.get(
                f"/sensory-vocabulary-lab/api/packs/{pack_id}"
            ).json()["pack"]
            assert pack["words"][0]["scenes"][0]["image"] == replacement
        assert client.get(
            f"/sensory-vocabulary-lab{replacement}"
        ).headers["content-type"] == "image/webp"


def test_whole_entry_failure_keeps_previous_content(monkeypatch):
    calls: list[tuple[tuple[str, ...], str]] = []
    monkeypatch.setattr(vocab_app.vocab, "plan_wordpack", fake_plan(calls))
    word = "cacheprobegamma"

    with TestClient(app, base_url="https://testserver") as client:
        login(client)
        pack = client.post(
            "/sensory-vocabulary-lab/api/packs",
            json={"words": [word], "level": "IELTS"},
        ).json()["pack"]
        entry = pack["words"][0]
        original_core = entry["coreSenseCn"]

        def failing_image(**_kwargs):
            raise vocab_app.vocab.ServiceError(
                502,
                "test_image_failure",
                "test failure",
            )

        monkeypatch.setattr(
            vocab_app.vocab,
            "plan_wordpack",
            lambda words, level, _key: {
                "words": [word_payload(words[0], core="不应写入的新释义")]
            },
        )
        monkeypatch.setattr(vocab_app, "generated_image_bytes", failing_image)
        response = client.post(
            f"/sensory-vocabulary-lab/api/entries/{entry['id']}/regenerate",
            json={"confirm": True},
        )
        assert response.status_code == 502
        unchanged = client.get(
            f"/sensory-vocabulary-lab/api/packs/{pack['id']}"
        ).json()["pack"]
        assert unchanged["words"][0]["coreSenseCn"] == original_core
        assert unchanged["words"][0]["revision"] == 1


def test_whole_entry_success_replaces_all_references_atomically(monkeypatch):
    calls: list[tuple[tuple[str, ...], str]] = []
    monkeypatch.setattr(vocab_app.vocab, "plan_wordpack", fake_plan(calls))
    word = "cacheprobedelta"

    with TestClient(app, base_url="https://testserver") as client:
        login(client)
        first = client.post(
            "/sensory-vocabulary-lab/api/packs",
            json={"words": [word], "level": "TOEFL"},
        ).json()["pack"]
        second = client.post(
            "/sensory-vocabulary-lab/api/packs",
            json={"words": [word], "level": "TOEFL"},
        ).json()["pack"]
        entry_id = first["words"][0]["id"]
        monkeypatch.setattr(
            vocab_app.vocab,
            "plan_wordpack",
            lambda words, level, _key: {
                "words": [word_payload(words[0], core="已经原子替换的新释义")]
            },
        )
        staged_colors = iter(["#2e7f54", "#df7a29"])
        monkeypatch.setattr(
            vocab_app,
            "generated_image_bytes",
            lambda **_kwargs: (
                png_bytes(next(staged_colors)),
                {"cached": False},
            ),
        )
        regenerated = client.post(
            f"/sensory-vocabulary-lab/api/entries/{entry_id}/regenerate",
            json={"confirm": True},
        )
        assert regenerated.status_code == 200, regenerated.text
        assert regenerated.json()["affectedPacks"] == 2
        replacement = regenerated.json()["entry"]
        assert replacement["coreSenseCn"] == "已经原子替换的新释义"
        assert replacement["revision"] == 2
        assert all(scene["image"] for scene in replacement["scenes"])

        for pack_id in (first["id"], second["id"]):
            pack = client.get(
                f"/sensory-vocabulary-lab/api/packs/{pack_id}"
            ).json()["pack"]
            assert pack["words"][0]["revision"] == 2
            assert pack["words"][0]["coreSenseCn"] == "已经原子替换的新释义"


def test_legacy_import_is_idempotent(monkeypatch):
    word = "legacycacheprobe"
    legacy = {
        "id": "legacy-pack-1",
        "title": "旧版词包",
        "level": "PET",
        "date": "2026/07/29",
        "source": "live",
        "words": [word_payload(word)],
    }
    with TestClient(app, base_url="https://testserver") as client:
        login(client)
        first = client.post(
            "/sensory-vocabulary-lab/api/import/legacy",
            json={"packs": [legacy]},
        )
        assert first.status_code == 200, first.text
        assert first.json()["importedPacks"] == 1
        second = client.post(
            "/sensory-vocabulary-lab/api/import/legacy",
            json={"packs": [legacy]},
        )
        assert second.status_code == 200
        assert second.json()["importedPacks"] == 0
        assert second.json()["skippedPacks"] == 1
