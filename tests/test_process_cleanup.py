"""Process helper regression; fake pipes only, no candidate execution."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from agentforge.runtime import Runtime, RuntimeErrorSafe


class FakeProcess:
    def __init__(self):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode = None
        self.finished = asyncio.Event()

    async def wait(self):
        await self.finished.wait()
        return self.returncode

    def kill(self):
        self.returncode = -1
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.finished.set()


class ProcessCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_output_limit_kills_process_and_cancels_other_readers(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('agentforge.runtime._settings', return_value={}):
                runtime = Runtime(Path(directory), Path(directory)/'data')
            process = FakeProcess()
            process.stdout.feed_data(b'x' * 131073)
            existing = set(asyncio.all_tasks())
            with patch('asyncio.create_subprocess_exec', AsyncMock(return_value=process)):
                with self.assertRaises(RuntimeErrorSafe):
                    await runtime._command(['fake-process'], timeout=2)
            self.assertEqual(process.returncode, -1)
            self.assertFalse([t for t in asyncio.all_tasks() - existing if not t.done()])

    async def test_timeout_kills_and_reaps(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('agentforge.runtime._settings', return_value={}):
                runtime = Runtime(Path(directory), Path(directory)/'data')
            process = FakeProcess()
            with patch('asyncio.create_subprocess_exec', AsyncMock(return_value=process)):
                with self.assertRaises(TimeoutError):
                    await runtime._command(['fake-process'], timeout=0.01)
            self.assertTrue(process.finished.is_set())
