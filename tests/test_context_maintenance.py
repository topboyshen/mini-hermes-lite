import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from mini_hermes.pi_channel.context_maintenance import context_tokens
from mini_hermes.pi_channel.broker import PiChannelBroker

import os

# Tests exercise the deployed contract: the unit declares the model explicitly.
os.environ.setdefault("PI_CHANNEL_MODEL", "xai/grok-4.6")


def usage_entry(tokens, identity='a', parent=None, **fields):
    return {'id': identity, 'parentId': parent, 'type': 'message', 'message': {
        'role': 'assistant', 'stopReason': 'stop', 'usage': {'input': tokens, **fields},
    }}


class ContextMetadataTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.session = Path(self.directory.name) / 'session.jsonl'

    def save(self, entries):
        self.session.write_text('\n'.join(json.dumps(entry) for entry in entries))

    def test_cached_tokens_count_toward_context(self):
        self.save([usage_entry(1000, output=100, cacheRead=90000)])
        self.assertEqual(context_tokens(self.session), 91100)

    def test_compaction_invalidates_old_usage_until_next_answer(self):
        self.save([usage_entry(93000), {'type': 'compaction', 'id': 'b', 'parentId': 'a'},
                   {'type': 'label', 'id': 'c', 'parentId': 'b'}])
        self.assertIsNone(context_tokens(self.session))
        with self.session.open('a') as stream:
            stream.write('\n' + json.dumps(usage_entry(22000, identity='d', parent='c')))
        self.assertEqual(context_tokens(self.session), 22000)

    def test_active_branch_not_unrelated_recent_branch(self):
        self.save([usage_entry(20000), usage_entry(99000, identity='b', parent='a'),
                   {'type': 'label', 'id': 'c', 'parentId': 'a'}])
        self.assertEqual(context_tokens(self.session), 20000)

    def test_malformed_missing_and_bad_usage_are_unknown(self):
        self.assertIsNone(context_tokens(self.session))
        self.session.write_text('malformed')
        self.assertIsNone(context_tokens(self.session))
        for value in (True, -1, 'secret', None):
            self.save([usage_entry(value)])
            self.assertIsNone(context_tokens(self.session))

    def test_cycles_and_failed_assistant_are_ignored(self):
        self.save([{'id': 'a', 'parentId': 'a', 'type': 'label'}])
        self.assertIsNone(context_tokens(self.session))
        failed = usage_entry(99000, identity='b', parent='a')
        failed['message']['stopReason'] = 'error'
        self.save([usage_entry(20000), failed])
        self.assertEqual(context_tokens(self.session), 20000)


class RpcProcess:
    def __init__(self, *, success=True, blocking=False):
        self.returncode = None
        self.stdout = asyncio.StreamReader()
        self.written = []
        self.stdin = SimpleNamespace(write=self.written.append, drain=AsyncMock(), close=lambda: None)
        if not blocking:
            self.stdout.feed_data((json.dumps({'type': 'response', 'id': 'hermes-context',
                'command': 'compact', 'success': success, 'error': 'SYNTHETIC_SECRET',
                'data': {'summary': 'SYNTHETIC_PRIVATE_SUMMARY', 'estimatedTokensAfter': 22000}}) + '\n').encode())
            self.stdout.feed_eof()

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.returncode = -15
        self.stdout.feed_eof()

    def kill(self):
        self.terminate()


class MaintenanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.session = Path(self.directory.name) / 'session.jsonl'
        self.session.write_text(json.dumps(usage_entry(93000)))
        self.broker = PiChannelBroker()
        self.broker.workspace = Path(self.directory.name)
        self.broker._session_path = lambda user_id: self.session
        self.events = []
        async def send(writer, event):
            self.events.append(event)
        self.broker._send = send

    async def test_native_compaction_only_exposes_metrics_and_disables_tools(self):
        process = RpcProcess()
        original = self.session.read_bytes()
        with patch('asyncio.create_subprocess_exec', AsyncMock(return_value=process)) as spawn:
            with self.assertLogs('mini_hermes.pi_channel.context_maintenance', level='INFO') as logs:
                result = await self.broker._maintain_context(42, object())
        self.assertEqual(result, {'status': 'compacted', 'tokens_before': 93000, 'tokens_after': 22000})
        for flag in ('--offline', '--no-tools', '--no-extensions', '--no-skills'):
            self.assertIn(flag, spawn.call_args.args)
        self.assertIn('rpc', spawn.call_args.args)
        self.assertEqual(json.loads(process.written[0])['type'], 'compact')
        self.assertNotIn('SYNTHETIC', str(result) + str(self.events) + str(logs.output))
        self.assertEqual(self.session.read_bytes(), original, 'only native pi may append a compaction')
        self.assertIsNone(self.broker._process)

    async def test_small_or_already_compacted_session_skips_process(self):
        self.session.write_text(json.dumps(usage_entry(22000)))
        with patch('asyncio.create_subprocess_exec', AsyncMock()) as spawn:
            result = await self.broker._maintain_context(42, object())
        self.assertEqual(result['status'], 'not_needed')
        spawn.assert_not_awaited()
        self.assertEqual(self.events, [])

    async def test_failure_has_cooldown_and_keeps_original(self):
        original = self.session.read_bytes()
        with patch('asyncio.create_subprocess_exec', AsyncMock(return_value=RpcProcess(success=False))) as spawn:
            self.assertEqual((await self.broker._maintain_context(42, object()))['status'], 'failed')
            self.assertEqual((await self.broker._maintain_context(42, object()))['status'], 'not_needed')
            spawn.assert_awaited_once()
        self.assertEqual(self.session.read_bytes(), original)

    async def test_timeout_cleans_up_process(self):
        process = RpcProcess(blocking=True)
        with patch('asyncio.create_subprocess_exec', AsyncMock(return_value=process)), \
                patch('mini_hermes.pi_channel.context_maintenance.COMPACT_TIMEOUT', 0.01):
            result = await self.broker._maintain_context(42, object())
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(process.returncode, -15)
        self.assertIsNone(self.broker._process)

    async def test_abort_during_maintenance_does_not_start_prompt(self):
        async def cancelled(*args, **kwargs):
            self.broker._cancel_requested = True
        self.broker._maintain_context = AsyncMock(side_effect=cancelled)
        self.broker._run_prompt_process = AsyncMock()
        await self.broker._run_prompt(42, 'synthetic', object())
        self.broker._run_prompt_process.assert_not_awaited()
        self.assertEqual(self.events[-1]['error_code'], 'cancelled')

    async def test_manual_maintenance_requires_authorized_local_peer(self):
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"action":"compact","user_id":42}\n')
        writer = SimpleNamespace(close=lambda: None, wait_closed=AsyncMock())
        self.broker._authorize_research = Mock(side_effect=ValueError('denied'))
        self.broker._maintain_context = AsyncMock()
        await self.broker._handle(reader, writer)
        self.broker._maintain_context.assert_not_awaited()
        self.assertEqual(self.events[-1]['error_code'], 'invalid_request')

    async def test_manual_maintenance_never_interrupts_busy_channel(self):
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"action":"compact","user_id":42}\n')
        writer = SimpleNamespace(close=lambda: None, wait_closed=AsyncMock())
        self.broker._authorize_research = Mock()
        self.broker._maintain_context = AsyncMock()
        async with self.broker._lock:
            await asyncio.wait_for(self.broker._handle(reader, writer), 0.2)
        self.broker._maintain_context.assert_not_awaited()
        self.assertEqual(self.events[-1]['error_code'], 'channel_busy')


if __name__ == '__main__':
    unittest.main()
