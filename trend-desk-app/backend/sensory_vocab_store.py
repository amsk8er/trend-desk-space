"""Persistent shared library for the mounted Sensory Vocabulary Lab.

The vocabulary tables intentionally use their own metadata lifecycle.  Trend
Desk's production schema is managed by a private Alembic pipeline, while this
public portal repository must be able to add the isolated ``vocab_*`` tables
without changing or introspecting any financial tables.
"""

from __future__ import annotations

import hashlib
import unicodedata
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Column, JSON, LargeBinary, UniqueConstraint, delete, func, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Field, SQLModel, Session, select

from backend.engine import engine


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def normalize_word(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def normalize_level(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().upper()


class VocabEntry(SQLModel, table=True):
    __tablename__ = "vocab_entry"
    __table_args__ = (
        UniqueConstraint(
            "normalized_word",
            "level",
            name="ux_vocab_entry_word_level",
        ),
    )

    entry_id: str = Field(primary_key=True)
    normalized_word: str = Field(index=True)
    display_word: str
    level: str = Field(index=True)
    content_json: dict = Field(sa_column=Column(JSON, nullable=False))
    planner_model: str = ""
    prompt_version: str = ""
    revision: int = 1
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class VocabImage(SQLModel, table=True):
    __tablename__ = "vocab_image"
    __table_args__ = (
        UniqueConstraint("sha256", name="ux_vocab_image_sha256"),
    )

    image_id: str = Field(primary_key=True)
    sha256: str = Field(index=True)
    mime_type: str = "image/webp"
    width: int
    height: int
    byte_size: int
    data: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    image_model: str = ""
    style_version: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class VocabScene(SQLModel, table=True):
    __tablename__ = "vocab_scene"
    __table_args__ = (
        UniqueConstraint(
            "entry_id",
            "position",
            name="ux_vocab_scene_entry_position",
        ),
    )

    scene_id: str = Field(primary_key=True)
    entry_id: str = Field(foreign_key="vocab_entry.entry_id", index=True)
    position: int
    content_json: dict = Field(sa_column=Column(JSON, nullable=False))
    image_id: str | None = Field(
        default=None,
        foreign_key="vocab_image.image_id",
        index=True,
    )
    image_revision: int = 0
    updated_at: datetime = Field(default_factory=utcnow)


class VocabPack(SQLModel, table=True):
    __tablename__ = "vocab_pack"

    pack_id: str = Field(primary_key=True)
    title: str
    level: str = Field(index=True)
    source: str = "live"
    display_date: str
    import_fingerprint: str | None = Field(default=None, index=True, unique=True)
    created_at: datetime = Field(default_factory=utcnow, index=True)
    updated_at: datetime = Field(default_factory=utcnow)


class VocabPackItem(SQLModel, table=True):
    __tablename__ = "vocab_pack_item"
    __table_args__ = (
        UniqueConstraint(
            "pack_id",
            "position",
            name="ux_vocab_pack_item_position",
        ),
    )

    item_id: int | None = Field(default=None, primary_key=True)
    pack_id: str = Field(foreign_key="vocab_pack.pack_id", index=True)
    entry_id: str = Field(foreign_key="vocab_entry.entry_id", index=True)
    position: int


class VocabGenerationLease(SQLModel, table=True):
    __tablename__ = "vocab_generation_lease"

    cache_key: str = Field(primary_key=True)
    owner_token: str
    expires_at: datetime = Field(index=True)
    created_at: datetime = Field(default_factory=utcnow)


class VocabAccessFailure(SQLModel, table=True):
    __tablename__ = "vocab_access_failure"

    visitor_hash: str = Field(primary_key=True)
    failures: int = 0
    window_started_at: datetime = Field(default_factory=utcnow)
    blocked_until: datetime | None = Field(default=None, index=True)
    updated_at: datetime = Field(default_factory=utcnow)


VOCAB_TABLES = [
    VocabEntry.__table__,
    VocabImage.__table__,
    VocabScene.__table__,
    VocabPack.__table__,
    VocabPackItem.__table__,
    VocabGenerationLease.__table__,
    VocabAccessFailure.__table__,
]


def ensure_schema() -> None:
    SQLModel.metadata.create_all(engine, tables=VOCAB_TABLES, checkfirst=True)


def entry_cache_key(word: str, level: str) -> str:
    return f"{normalize_level(level)}\n{normalize_word(word)}"


def find_entry(session: Session, word: str, level: str) -> VocabEntry | None:
    return session.exec(
        select(VocabEntry).where(
            VocabEntry.normalized_word == normalize_word(word),
            VocabEntry.level == normalize_level(level),
        )
    ).first()


def find_entries(
    session: Session,
    words: list[str],
    level: str,
) -> dict[str, VocabEntry]:
    normalized = [normalize_word(word) for word in words]
    if not normalized:
        return {}
    rows = session.exec(
        select(VocabEntry).where(
            VocabEntry.normalized_word.in_(normalized),
            VocabEntry.level == normalize_level(level),
        )
    ).all()
    return {row.normalized_word: row for row in rows}


def create_entry(
    session: Session,
    word_payload: dict[str, Any],
    level: str,
    *,
    planner_model: str,
    prompt_version: str,
) -> VocabEntry:
    display_word = str(word_payload.get("word") or "").strip()
    content = {
        key: value
        for key, value in word_payload.items()
        if key not in {"id", "scenes"}
    }
    entry = VocabEntry(
        entry_id=new_id("word"),
        normalized_word=normalize_word(display_word),
        display_word=display_word,
        level=normalize_level(level),
        content_json=content,
        planner_model=planner_model,
        prompt_version=prompt_version,
    )
    session.add(entry)
    session.flush()
    for position, scene_payload in enumerate(word_payload.get("scenes") or []):
        scene_content = {
            key: value
            for key, value in dict(scene_payload).items()
            if key not in {"id", "image", "imageSource"}
        }
        session.add(
            VocabScene(
                scene_id=new_id("scene"),
                entry_id=entry.entry_id,
                position=position,
                content_json=scene_content,
            )
        )
    session.flush()
    return entry


def serialize_entry(session: Session, entry: VocabEntry) -> dict[str, Any]:
    scenes = session.exec(
        select(VocabScene)
        .where(VocabScene.entry_id == entry.entry_id)
        .order_by(VocabScene.position)
    ).all()
    payload = dict(entry.content_json)
    payload.update(
        {
            "id": entry.entry_id,
            "word": entry.display_word,
            "level": entry.level,
            "revision": entry.revision,
            "referenceCount": count_entry_packs(session, entry.entry_id),
            "scenes": [
                {
                    **dict(scene.content_json),
                    "id": scene.scene_id,
                    "image": (
                        f"/api/images/{scene.image_id}"
                        if scene.image_id
                        else None
                    ),
                    "imageRevision": scene.image_revision,
                }
                for scene in scenes
            ],
        }
    )
    return payload


def serialize_pack(session: Session, pack: VocabPack) -> dict[str, Any]:
    items = session.exec(
        select(VocabPackItem)
        .where(VocabPackItem.pack_id == pack.pack_id)
        .order_by(VocabPackItem.position)
    ).all()
    entries = [
        session.get(VocabEntry, item.entry_id)
        for item in items
    ]
    return {
        "id": pack.pack_id,
        "title": pack.title,
        "level": pack.level,
        "date": pack.display_date,
        "source": pack.source,
        "createdAt": pack.created_at.isoformat() + "Z",
        "words": [
            serialize_entry(session, entry)
            for entry in entries
            if entry is not None
        ],
    }


def list_packs(limit: int = 24, offset: int = 0) -> tuple[list[dict[str, Any]], bool]:
    safe_limit = min(max(limit, 1), 50)
    safe_offset = max(offset, 0)
    with Session(engine) as session:
        rows = session.exec(
            select(VocabPack)
            .order_by(VocabPack.created_at.desc(), VocabPack.pack_id.desc())
            .offset(safe_offset)
            .limit(safe_limit + 1)
        ).all()
        has_more = len(rows) > safe_limit
        return [
            serialize_pack(session, row)
            for row in rows[:safe_limit]
        ], has_more


def get_pack(pack_id: str) -> dict[str, Any] | None:
    with Session(engine) as session:
        pack = session.get(VocabPack, pack_id)
        return serialize_pack(session, pack) if pack else None


def save_pack(
    session: Session,
    *,
    title: str,
    level: str,
    entries: list[VocabEntry],
    source: str = "live",
    display_date: str | None = None,
    import_fingerprint: str | None = None,
    created_at: datetime | None = None,
) -> VocabPack:
    pack = VocabPack(
        pack_id=new_id("pack"),
        title=title.strip() or ", ".join(entry.display_word for entry in entries),
        level=normalize_level(level),
        source=source,
        display_date=display_date or utcnow().strftime("%Y/%m/%d"),
        import_fingerprint=import_fingerprint,
        created_at=created_at or utcnow(),
        updated_at=created_at or utcnow(),
    )
    session.add(pack)
    session.flush()
    for position, entry in enumerate(entries):
        session.add(
            VocabPackItem(
                pack_id=pack.pack_id,
                entry_id=entry.entry_id,
                position=position,
            )
        )
    session.flush()
    return pack


def count_entry_packs(session: Session, entry_id: str) -> int:
    return int(
        session.exec(
            select(func.count(VocabPackItem.item_id)).where(
                VocabPackItem.entry_id == entry_id
            )
        ).one()
    )


def claim_generation_leases(
    words: list[str],
    level: str,
    owner_token: str,
    *,
    lease_seconds: int = 180,
) -> tuple[list[str], list[str]]:
    now = utcnow()
    expires = now + timedelta(seconds=lease_seconds)
    owned: list[str] = []
    waiting: list[str] = []
    with Session(engine) as session:
        for word in words:
            cache_key = entry_cache_key(word, level)
            values = {
                "cache_key": cache_key,
                "owner_token": owner_token,
                "expires_at": expires,
                "created_at": now,
            }
            insert_factory = (
                postgres_insert
                if engine.dialect.name == "postgresql"
                else sqlite_insert
            )
            inserted = session.exec(
                insert_factory(VocabGenerationLease.__table__)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["cache_key"])
            )
            session.commit()
            if inserted.rowcount:
                owned.append(word)
                continue
            claimed = session.exec(
                update(VocabGenerationLease)
                .where(
                    VocabGenerationLease.cache_key == cache_key,
                    (
                        (VocabGenerationLease.expires_at <= now)
                        | (VocabGenerationLease.owner_token == owner_token)
                    ),
                )
                .values(
                    owner_token=owner_token,
                    expires_at=expires,
                    created_at=now,
                )
            )
            session.commit()
            if claimed.rowcount:
                owned.append(word)
            else:
                waiting.append(word)
    return owned, waiting


def release_generation_leases(owner_token: str) -> None:
    with Session(engine) as session:
        session.exec(
            delete(VocabGenerationLease).where(
                VocabGenerationLease.owner_token == owner_token
            )
        )
        session.commit()


def get_scene(scene_id: str) -> tuple[VocabScene, VocabEntry] | None:
    with Session(engine) as session:
        scene = session.get(VocabScene, scene_id)
        if scene is None:
            return None
        entry = session.get(VocabEntry, scene.entry_id)
        if entry is None:
            return None
        session.expunge(scene)
        session.expunge(entry)
        return scene, entry


def get_image(image_id: str) -> VocabImage | None:
    with Session(engine) as session:
        image = session.get(VocabImage, image_id)
        if image is not None:
            session.expunge(image)
        return image


def upsert_image(
    session: Session,
    *,
    data: bytes,
    width: int,
    height: int,
    image_model: str,
    style_version: str,
) -> VocabImage:
    digest = hashlib.sha256(data).hexdigest()
    image = session.exec(
        select(VocabImage).where(VocabImage.sha256 == digest)
    ).first()
    if image:
        return image
    image = VocabImage(
        image_id=new_id("image"),
        sha256=digest,
        width=width,
        height=height,
        byte_size=len(data),
        data=data,
        image_model=image_model,
        style_version=style_version,
    )
    session.add(image)
    session.flush()
    return image


def delete_image_if_unreferenced(session: Session, image_id: str | None) -> None:
    if not image_id:
        return
    references = session.exec(
        select(func.count(VocabScene.scene_id)).where(
            VocabScene.image_id == image_id
        )
    ).one()
    if int(references) == 0:
        image = session.get(VocabImage, image_id)
        if image is not None:
            session.delete(image)
