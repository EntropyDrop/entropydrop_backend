"""Compact, revisioned far-surface snapshots for the wrapped Space world."""

import asyncio
import hashlib
import json
import logging
import math
import struct
import threading
import time

import zstandard as zstd
from sqlalchemy import func

import models
from database import SessionLocal


SURFACE_MAGIC = b"EDSZ"
SURFACE_SCHEMA_VERSION = 2
SURFACE_SAMPLES_PER_CHUNK_AXIS = 8
SURFACE_RECORD_BYTES = 5
SURFACE_HEADER_BYTES = 32
SURFACE_CODEC_ZSTD = 1
SURFACE_COLOR = 0x718F61
MIDDLE_COLOR = 0x806B5C
DEEP_COLOR = 0x66707D
SURFACE_JOB_IDLE_SECONDS = 30

logger = logging.getLogger(__name__)
_generation_thread_lock = threading.Lock()
_generation_thread: threading.Thread | None = None

_GRAD3 = (
    (1, 1, 0), (-1, 1, 0), (1, -1, 0), (-1, -1, 0),
    (1, 0, 1), (-1, 0, 1), (1, 0, -1), (-1, 0, -1),
    (0, 1, 1), (0, -1, 1), (0, 1, -1), (0, -1, -1),
)


class _SimplexNoise3D:
    """Port of simplex-noise 4.x's seeded 3D path used by the browser."""

    def __init__(self, seed: int):
        state = seed
        permutation = list(range(256))
        for index in range(255):
            state = (state * 9301 + 49297) % 233280
            random_value = state / 233280
            swap_index = index + int(random_value * (256 - index))
            permutation[index], permutation[swap_index] = permutation[swap_index], permutation[index]
        self.permutation = permutation + permutation

    def __call__(self, x: float, y: float, z: float) -> float:
        skew = (x + y + z) / 3
        i = math.floor(x + skew)
        j = math.floor(y + skew)
        k = math.floor(z + skew)
        unskew = (i + j + k) / 6
        x0, y0, z0 = x - (i - unskew), y - (j - unskew), z - (k - unskew)

        if x0 >= y0:
            if y0 >= z0:
                i1, j1, k1, i2, j2, k2 = 1, 0, 0, 1, 1, 0
            elif x0 >= z0:
                i1, j1, k1, i2, j2, k2 = 1, 0, 0, 1, 0, 1
            else:
                i1, j1, k1, i2, j2, k2 = 0, 0, 1, 1, 0, 1
        elif y0 < z0:
            i1, j1, k1, i2, j2, k2 = 0, 0, 1, 0, 1, 1
        elif x0 < z0:
            i1, j1, k1, i2, j2, k2 = 0, 1, 0, 0, 1, 1
        else:
            i1, j1, k1, i2, j2, k2 = 0, 1, 0, 1, 1, 0

        offsets = (
            (x0, y0, z0, 0, 0, 0),
            (x0 - i1 + 1 / 6, y0 - j1 + 1 / 6, z0 - k1 + 1 / 6, i1, j1, k1),
            (x0 - i2 + 1 / 3, y0 - j2 + 1 / 3, z0 - k2 + 1 / 3, i2, j2, k2),
            (x0 - 0.5, y0 - 0.5, z0 - 0.5, 1, 1, 1),
        )
        ii, jj, kk = i & 255, j & 255, k & 255
        total = 0.0
        for ox, oy, oz, di, dj, dk in offsets:
            attenuation = 0.6 - ox * ox - oy * oy - oz * oz
            if attenuation < 0:
                continue
            gradient_index = self.permutation[
                ii + di + self.permutation[jj + dj + self.permutation[kk + dk]]
            ] % 12
            gx, gy, gz = _GRAD3[gradient_index]
            attenuation *= attenuation
            total += attenuation * attenuation * (gx * ox + gy * oy + gz * oz)
        return 32 * total


class TerrainSurfaceGenerator:
    def __init__(self, seed: int, width_cells: int, length_cells: int):
        self.noise = _SimplexNoise3D(seed)
        self.width_cells = width_cells
        self.length_cells = length_cells
        self.major_radius = width_cells / (2 * math.pi)
        self.minor_radius = length_cells / (2 * math.pi)

    def sample_height(self, world_x: int, world_z: int) -> int:
        theta = (world_x / self.width_cells) * math.tau
        phi = (world_z / self.length_cells) * math.tau
        cos_theta, sin_theta = math.cos(theta), math.sin(theta)
        cos_phi, sin_phi = math.cos(phi), math.sin(phi)
        px = (self.major_radius + self.minor_radius * cos_phi) * cos_theta
        py = (self.major_radius + self.minor_radius * cos_phi) * sin_theta
        pz = self.minor_radius * sin_phi
        broad = self.noise(px * 0.018, py * 0.018, pz * 0.018)
        detail = self.noise(px * 0.052, py * 0.052, pz * 0.052)
        height = math.floor(16 + broad * 3.4 + detail * 1.2 + 0.5)

        spawn_distance = math.hypot(
            world_x - self.width_cells / 2,
            world_z - self.length_cells / 2,
        )
        if spawn_distance < 26:
            blend = max(0.0, min(1.0, (spawn_distance - 10) / 16))
            height = math.floor(16 * (1 - blend) + height * blend + 0.5)
        return max(11, min(21, height))


def _decode_overlay(snapshot: models.SpaceChunkSnapshot) -> dict:
    if snapshot.codec == 0:
        encoded = bytes(snapshot.payload)
    elif snapshot.codec == 1:
        encoded = zstd.ZstdDecompressor().decompress(
            bytes(snapshot.payload),
            max_output_size=int(snapshot.uncompressed_size),
        )
    else:
        raise ValueError("unsupported terrain snapshot codec")
    if (
        len(encoded) != int(snapshot.uncompressed_size)
        or hashlib.sha256(encoded).digest() != bytes(snapshot.content_hash)
    ):
        raise ValueError("terrain snapshot checksum mismatch")
    payload = json.loads(encoded.decode("utf-8"))
    return {
        "standard": payload.get("standard", []),
        "micro": payload.get("micro", []),
    }


def _procedural_color(block_y: int, base_height: int) -> int:
    if block_y == base_height:
        return SURFACE_COLOR
    if block_y >= base_height - 3:
        return MIDDLE_COLOR
    return DEEP_COLOR


def _surface_records_for_chunk(
    generator: TerrainSurfaceGenerator,
    chunk_x: int,
    chunk_z: int,
    overlay: dict | None,
) -> list[tuple[int, int]]:
    axis = SURFACE_SAMPLES_PER_CHUNK_AXIS
    sample_width = 16 // axis
    standard_by_column: dict[tuple[int, int], dict[int, tuple[int, int]]] = {}
    standard_candidates: dict[int, list[tuple[int, int]]] = {}
    micro_candidates: dict[int, list[tuple[int, int]]] = {}
    if overlay:
        for edit in overlay["standard"]:
            if not isinstance(edit, list) or len(edit) < 5:
                continue
            world_x, y, world_z, block, color = map(int, edit[:5])
            standard_by_column.setdefault((world_x, world_z), {})[y] = (block, color)
            local_x, local_z = world_x - chunk_x * 16, world_z - chunk_z * 16
            if 0 <= local_x < 16 and 0 <= local_z < 16 and block == 1:
                patch = (local_x // sample_width) * axis + local_z // sample_width
                standard_candidates.setdefault(patch, []).append(((y + 1) * 5, color))
        for edit in overlay["micro"]:
            if not isinstance(edit, list) or len(edit) < 4:
                continue
            micro_x, micro_y, micro_z, color = map(int, edit[:4])
            world_x, world_z = micro_x // 5, micro_z // 5
            local_x, local_z = world_x - chunk_x * 16, world_z - chunk_z * 16
            if 0 <= local_x < 16 and 0 <= local_z < 16:
                patch = (local_x // sample_width) * axis + local_z // sample_width
                micro_candidates.setdefault(patch, []).append((micro_y + 1, color))

    records: list[tuple[int, int]] = []
    for sample_x in range(axis):
        for sample_z in range(axis):
            world_x = chunk_x * 16 + sample_x * sample_width + sample_width // 2
            world_z = chunk_z * 16 + sample_z * sample_width + sample_width // 2
            base_height = generator.sample_height(world_x, world_z)
            column_edits = standard_by_column.get((world_x, world_z), {})
            top_block_y = base_height
            while top_block_y >= 0 and column_edits.get(top_block_y, (1, 0))[0] == 0:
                top_block_y -= 1
            explicit_top = column_edits.get(top_block_y)
            top_color = (
                explicit_top[1]
                if explicit_top is not None and explicit_top[0] == 1
                else _procedural_color(top_block_y, base_height)
            )
            for edited_y, (block, color) in column_edits.items():
                if block == 1 and edited_y >= top_block_y:
                    top_block_y, top_color = edited_y, color
            height_micro = (top_block_y + 1) * 5 if top_block_y >= 0 else 0
            patch = sample_x * axis + sample_z
            for candidate_height, candidate_color in standard_candidates.get(patch, ()):
                if candidate_height >= height_micro:
                    height_micro, top_color = candidate_height, candidate_color
            for candidate_height, candidate_color in micro_candidates.get(patch, ()):
                if candidate_height >= height_micro:
                    height_micro, top_color = candidate_height, candidate_color
            records.append((height_micro, top_color))
    return records


def build_surface_zone_payload(
    world: models.SpaceWorld,
    zone_x: int,
    zone_z: int,
    source_terrain_revision: int,
    overlays: dict[tuple[int, int], dict] | None = None,
) -> bytes:
    zone_size = int(world.zone_size_chunks)
    record_count = zone_size * zone_size * SURFACE_SAMPLES_PER_CHUNK_AXIS ** 2
    payload = bytearray(SURFACE_HEADER_BYTES + record_count * SURFACE_RECORD_BYTES)
    struct.pack_into(
        "<4sBBBBHHiIQI",
        payload,
        0,
        SURFACE_MAGIC,
        SURFACE_SCHEMA_VERSION,
        SURFACE_SAMPLES_PER_CHUNK_AXIS,
        zone_size,
        SURFACE_RECORD_BYTES,
        zone_x,
        zone_z,
        int(world.seed),
        int(world.terrain_generator_version),
        int(source_terrain_revision),
        record_count,
    )
    generator = TerrainSurfaceGenerator(
        int(world.seed),
        int(world.width_chunks) * 16,
        int(world.length_chunks) * 16,
    )
    offset = SURFACE_HEADER_BYTES
    for local_chunk_x in range(zone_size):
        chunk_x = zone_x * zone_size + local_chunk_x
        for local_chunk_z in range(zone_size):
            chunk_z = zone_z * zone_size + local_chunk_z
            records = _surface_records_for_chunk(
                generator,
                chunk_x,
                chunk_z,
                (overlays or {}).get((chunk_x, chunk_z)),
            )
            for height_micro, color in records:
                struct.pack_into(
                    "<HBBB",
                    payload,
                    offset,
                    height_micro,
                    (color >> 16) & 0xFF,
                    (color >> 8) & 0xFF,
                    color & 0xFF,
                )
                offset += SURFACE_RECORD_BYTES
    return bytes(payload)


def decode_surface_zone_row(row: models.SpaceSurfaceZoneSnapshot) -> bytes:
    if row.codec != SURFACE_CODEC_ZSTD:
        raise ValueError("unsupported surface snapshot codec")
    payload = zstd.ZstdDecompressor().decompress(
        bytes(row.payload),
        max_output_size=int(row.uncompressed_size),
    )
    if (
        len(payload) != int(row.uncompressed_size)
        or hashlib.sha256(payload).digest() != bytes(row.content_hash)
    ):
        raise ValueError("surface snapshot checksum mismatch")
    return payload


def generate_surface_zone(db, world: models.SpaceWorld, zone_x: int, zone_z: int):
    zone_size = int(world.zone_size_chunks)
    min_x, min_z = zone_x * zone_size, zone_z * zone_size
    max_x, max_z = min_x + zone_size, min_z + zone_size
    overlay_rows = db.query(models.SpaceChunkSnapshot).filter(
        models.SpaceChunkSnapshot.world_id == world.id,
        models.SpaceChunkSnapshot.chunk_x >= min_x,
        models.SpaceChunkSnapshot.chunk_x < max_x,
        models.SpaceChunkSnapshot.chunk_z >= min_z,
        models.SpaceChunkSnapshot.chunk_z < max_z,
    ).all()
    source_revision = max((int(row.last_event_id or 0) for row in overlay_rows), default=0)
    overlays = {
        (int(row.chunk_x), int(row.chunk_z)): _decode_overlay(row)
        for row in overlay_rows
    }
    raw = build_surface_zone_payload(world, zone_x, zone_z, source_revision, overlays)
    compressed = zstd.ZstdCompressor(level=6).compress(raw)
    digest = hashlib.sha256(raw).digest()

    row = db.query(models.SpaceSurfaceZoneSnapshot).filter(
        models.SpaceSurfaceZoneSnapshot.world_id == world.id,
        models.SpaceSurfaceZoneSnapshot.zone_x == zone_x,
        models.SpaceSurfaceZoneSnapshot.zone_z == zone_z,
    ).with_for_update().first()
    # Lock the destination before the final source check. Terrain mutation
    # transactions lock the same row before marking it dirty, so an edit can
    # never be silently overwritten by a stale rebuild of an existing zone.
    current_revision = db.query(func.max(models.SpaceChunkSnapshot.last_event_id)).filter(
        models.SpaceChunkSnapshot.world_id == world.id,
        models.SpaceChunkSnapshot.chunk_x >= min_x,
        models.SpaceChunkSnapshot.chunk_x < max_x,
        models.SpaceChunkSnapshot.chunk_z >= min_z,
        models.SpaceChunkSnapshot.chunk_z < max_z,
    ).scalar() or 0
    if int(current_revision) != source_revision:
        db.rollback()
        return None
    if row is None:
        row = models.SpaceSurfaceZoneSnapshot(
            world_id=world.id,
            zone_x=zone_x,
            zone_z=zone_z,
            revision=1,
        )
        db.add(row)
    else:
        row.revision = int(row.revision or 0) + 1
    row.source_terrain_revision = source_revision
    row.terrain_generator_version = int(world.terrain_generator_version)
    row.schema_version = SURFACE_SCHEMA_VERSION
    row.samples_per_chunk_axis = SURFACE_SAMPLES_PER_CHUNK_AXIS
    row.codec = SURFACE_CODEC_ZSTD
    row.uncompressed_size = len(raw)
    row.content_hash = digest
    row.payload = compressed
    row.dirty = False
    db.commit()
    db.refresh(row)
    # A first build has no pre-existing row for the mutation transaction to
    # lock. Recheck after publishing it; a concurrent edit either marks the new
    # row itself or is detected here and schedules another rebuild.
    latest_revision = db.query(func.max(models.SpaceChunkSnapshot.last_event_id)).filter(
        models.SpaceChunkSnapshot.world_id == world.id,
        models.SpaceChunkSnapshot.chunk_x >= min_x,
        models.SpaceChunkSnapshot.chunk_x < max_x,
        models.SpaceChunkSnapshot.chunk_z >= min_z,
        models.SpaceChunkSnapshot.chunk_z < max_z,
    ).scalar() or 0
    if int(latest_revision) != source_revision:
        row.dirty = True
        db.commit()
        db.refresh(row)
    return row


def generate_next_surface_zone() -> bool:
    db = SessionLocal()
    try:
        # This lock also makes the API self-healing worker safe alongside the
        # normal Redis-singleton background service and across API replicas.
        worlds = db.query(models.SpaceWorld).filter(
            models.SpaceWorld.status == 1
        ).with_for_update(skip_locked=True).all()
        for world in worlds:
            dirty = db.query(models.SpaceSurfaceZoneSnapshot).filter(
                models.SpaceSurfaceZoneSnapshot.world_id == world.id,
                (
                    (models.SpaceSurfaceZoneSnapshot.dirty.is_(True))
                    | (models.SpaceSurfaceZoneSnapshot.terrain_generator_version != world.terrain_generator_version)
                    | (models.SpaceSurfaceZoneSnapshot.schema_version != SURFACE_SCHEMA_VERSION)
                    | (
                        models.SpaceSurfaceZoneSnapshot.samples_per_chunk_axis
                        != SURFACE_SAMPLES_PER_CHUNK_AXIS
                    )
                    | (models.SpaceSurfaceZoneSnapshot.codec != SURFACE_CODEC_ZSTD)
                ),
            ).order_by(models.SpaceSurfaceZoneSnapshot.updated_at.asc()).first()
            if dirty is not None:
                generate_surface_zone(db, world, int(dirty.zone_x), int(dirty.zone_z))
                return True

            existing = {
                (int(row.zone_x), int(row.zone_z))
                for row in db.query(models.SpaceSurfaceZoneSnapshot).filter(
                    models.SpaceSurfaceZoneSnapshot.world_id == world.id,
                ).all()
            }
            zones_x = int(world.width_chunks) // int(world.zone_size_chunks)
            zones_z = int(world.length_chunks) // int(world.zone_size_chunks)
            for zone_x in range(zones_x):
                for zone_z in range(zones_z):
                    if (zone_x, zone_z) in existing:
                        continue
                    generate_surface_zone(db, world, zone_x, zone_z)
                    return True
        return False
    finally:
        db.close()


def _run_surface_generation_until_current() -> None:
    try:
        while generate_next_surface_zone():
            time.sleep(0.05)
    except Exception:
        logger.exception("Space surface snapshot generation failed")


def ensure_surface_generation_started() -> bool:
    """Start one daemon backfill in this process if another is not running."""
    global _generation_thread
    with _generation_thread_lock:
        if _generation_thread is not None and _generation_thread.is_alive():
            return False
        _generation_thread = threading.Thread(
            target=_run_surface_generation_until_current,
            name="space-surface-snapshot-warmup",
            daemon=True,
        )
        _generation_thread.start()
        return True


async def start_surface_snapshot_job() -> None:
    while True:
        generated = await asyncio.to_thread(generate_next_surface_zone)
        await asyncio.sleep(0.05 if generated else SURFACE_JOB_IDLE_SECONDS)
