import sys
import shutil
from pathlib import Path
import re
import json
import asyncio
from multiprocessing import Pool, Lock
from collections import defaultdict

from anvil import EmptyRegion, EmptyChunk, Block
import nbtlib
from nbtlib.tag import String
from tqdm import tqdm

from .classes import World, Entry, parse_position
from .parser import parser

OVERWORLD = 0
NETHER = 1
END = 2


class ChunkConverter:
    def __init__(
        self,
        position: tuple[int, int, int],
        entry: Entry,
        blocks: dict,
        corrupted: bool = False,
    ) -> None:
        self.region_x, self.region_z, self.dimension = position
        self.entry = entry
        self.blocks = blocks
        self.corrupted = corrupted

    corrupted_block = Block("minecraft", "glass")

    def place_blocks(self) -> None:
        self.chunk = EmptyChunk(self.region_x, self.region_z)
        if False and self.corrupted:
            for subchunk_y in range(8):
                y_offset = subchunk_y * 16
                for position in range(0x1000):
                    y = (position & 0xF) + y_offset
                    z = (position & 0xF0) >> 4
                    x = position >> 8
                    self.chunk.set_block(self.corrupted_block, x, y, z)
        else:
            for subchunk_y, blocks in enumerate(self.entry.blocks):
                y_offset = subchunk_y * 16
                for position, block in enumerate(blocks[:0x1000]):
                    y = (position & 0xF) + y_offset
                    z = (position & 0xF0) >> 4
                    x = position >> 8
                    # extract the nibble from the byte
                    block_byte = blocks[0x1000 + position // 2]
                    if position % 2 == 0:
                        block_data = block_byte & 0xF
                    else:
                        block_data = block_byte >> 4

                    block_id = (block, block_data)
                    if block_id != (0, 0):
                        try:
                            block = self.blocks[block_id]
                        except KeyError:
                            print(
                                f"unknown block {block_id} at {(x, y, z)} dimension {self.dimension}",
                                file=sys.stderr,
                            )
                            sys.stderr.flush()
                            block = Block("minecraft", "netherite_block")
                        self.chunk.set_block(block, x, y, z)

    @property
    def region_position(self) -> tuple[int, int, int]:
        return self.region_x // 32, self.region_z // 32, self.dimension


class RegionConverter:
    def __init__(self, world_directory: Path, position: tuple[int, int, int]) -> None:
        self.region_x, self.region_z, self.dimension = position
        self.world_directory = Path(world_directory)
        if self.dimension == OVERWORLD:
            dimension_path = self.world_directory
        elif self.dimension == NETHER:
            dimension_path = self.world_directory / "DIM-1"
        elif self.dimension == END:
            dimension_path = self.world_directory / "DIM1"
        else:
            raise ValueError("invalid dimension")
        self.region_file = (
            dimension_path / "region" / f"r.{self.region_x:d}.{self.region_z:d}.mca"
        )
        self.region = EmptyRegion(self.region_x, self.region_z)

    def add_chunk(self, chunk: EmptyChunk) -> None:
        self.region.add_chunk(chunk)

    def save(self) -> None:
        # if the region directory hasn't been generated yet, create it
        if self.world_directory.exists():
            self.region_file.parent.mkdir(parents=True, exist_ok=True)
        self.region.save(str(self.region_file))


def parse_block_json(raw_blocks: dict) -> dict:
    block_json = re.compile(r"^([^\[\]]+)(?:\[([^\[\]]*)\])?$")
    blocks = {}

    for numerical_id, new in raw_blocks["blocks"].items():
        block_str, data_str = numerical_id.split(":")
        block_id = (int(block_str), int(data_str))

        # convert minecraft:item[key=value,otherkey=value] to {"key": "value", "otherkey": "value"}
        parsed = block_json.match(new)
        if parsed is None:
            raise ValueError("invalid block")
        name = parsed[1]
        raw_nbt_data = parsed[2]
        # intentionally not using "is None" because it should match an empty string too
        if raw_nbt_data:
            nbt_data = dict(item.split("=") for item in raw_nbt_data.split(","))
        else:
            nbt_data = {}

        namespace, block = name.split(":")
        blocks[block_id] = Block(namespace, block, nbt_data)

    return blocks


def fix_cdb_index(path: Path, world: World) -> int:
    path = Path(path)
    recovered = world.recovered
    with open(path, "rb") as index_handle:
        index = parser.Index(index_handle)
    chunks_fixed = 0
    for entry_index, entry in enumerate(index.entries):
        position = parse_position(entry.position)
        try:
            chunk_copies = recovered[position]
        except KeyError:
            continue
        slot, subfile = chunk_copies[0]
        new_chunk_parameters = world.cdb[slot][subfile]._header.parameters
        entry.slot = slot
        entry.subfile = subfile
        entry.parameters = new_chunk_parameters
        chunks_fixed += 1
    if chunks_fixed:
        with open(path, "wb") as index_handle:
            index_handle.write(index.dumps())
    return chunks_fixed


def convert(
    world: World,
    blank_world: Path,
    world_out: Path,
    delete_out: bool = False,
) -> None:
    if world_out.exists():
        if delete_out:
            shutil.rmtree(world_out)
        else:
            raise FileExistsError("world output directory exists")
    shutil.copytree(blank_world, world_out)
    with nbtlib.load(world_out / "level.dat") as level:
        level["Data"]["LevelName"] = String(world.name)

    # read the JSON files containing MCPE block IDs
    with open(Path(__file__).parent / "data" / "blocks.json") as blocks_file:
        raw_blocks = json.load(blocks_file)
    blocks = parse_block_json(raw_blocks)

    if 1:
        # recover chunks
        world.recover_data()
        for position, found in world.recovered.items():
            total = len(found)
            recovered = ", ".join(
                f"(region {region:d} subfile {subfile:d})" for region, subfile in found
            )
            print(
                f"{position}: found {total:d} potentially non-corrupted copies: {recovered}"
            )
        # exit()

    chunk_converters = []

    if 1:
        for position, entry in world.entries.items():
            if entry is True:
                chunk_converters.append(ChunkConverter(position, None, blocks, True))
            else:
                chunk_converters.append(ChunkConverter(position, entry, blocks))
    if 1:
        for recover_position, recover_subfiles in world.recovered.items():
            slot, subfile = recover_subfiles[0]
            header = parser.CDBEntry(
                parser.Position(*recover_position),
                slot,
                subfile,
                0x20FF,
                0xA,
                0x1,
                0x8000,
            )
            new_entry = Entry(header, world.cdb[slot][subfile])
            chunk_converters.append(
                ChunkConverter(recover_position, new_entry, blocks, False)
            )
    chunks_fixed = fix_cdb_index(world._cdb_path / "newindex.cdb", world)
    if chunks_fixed:
        print(f"Fixed {chunks_fixed:d} corrupt chunks!")

    def convert_chunk(
        chunk_converter: ChunkConverter,
    ) -> tuple[tuple[int, int, int], EmptyChunk]:
        chunk_converter.place_blocks()
        return chunk_converter.region_position, chunk_converter.chunk

    regions = {}
    for chunk_converter in tqdm(
        chunk_converters, desc="Converting chunks", unit="chunk"
    ):
        region_position, chunk = convert_chunk(chunk_converter)
        try:
            region_converter = regions[region_position]
        except KeyError:
            region_converter = regions[region_position] = RegionConverter(
                world_out, region_position
            )
        region_converter.add_chunk(chunk)

    def save_region(region_converter: RegionConverter) -> None:
        region_converter.save()

    for region in tqdm(regions.values(), desc="Saving regions", unit="region"):
        save_region(region)
