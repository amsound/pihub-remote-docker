import asyncio

from pihub.input_unifying import UnifyingReader


def test_unifying_reader_drops_when_queue_full() -> None:
    async def _exercise() -> UnifyingReader:
        reader = UnifyingReader(
            device_path=None,
            scancode_map={},
            on_edge=lambda *_: None,
            edge_queue_maxsize=1,
        )
        reader._edge_queue = asyncio.Queue(maxsize=1)

        await reader._emit("rem_ok", "down")
        await reader._emit("rem_ok", "up")
        return reader

    reader = asyncio.run(_exercise())
    assert reader._dropped_edges == 1
