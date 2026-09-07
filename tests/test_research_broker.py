import asyncio
import json
from pathlib import Path
import socket
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from mini_hermes.pi_channel.broker import PiChannelBroker
from mini_hermes.pi_channel.client import MAX_MESSAGE_BYTES, PiChannelClient, PiChannelError


def message(text='research reply'):
    return {'role': 'assistant', 'content': [{'type': 'text', 'text': text}],
            'model': 'grok-4.6', 'provider': 'xai', 'api': 'openai-responses',
            'stopReason': 'stop', 'timestamp': 1, 'usage': {}}


class PeerTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(socket, 'SO_PEERCRED'), 'Linux peer credentials')
    def test_research_peer_is_root_or_main_bot_only(self):
        broker = PiChannelBroker()
        writer = Mock()
        connection = writer.get_extra_info.return_value
        with patch('mini_hermes.pi_channel.broker.pwd.getpwnam', return_value=SimpleNamespace(pw_uid=1001)):
            for uid in [0, 1001]:
                connection.getsockopt.return_value = struct.pack('3i', 1, uid, 99)
                broker._authorize_research(writer)
            connection.getsockopt.return_value = struct.pack('3i', 1, 1002, 99)
            with self.assertRaisesRegex(ValueError, 'not authorized'):
                broker._authorize_research(writer)


class ResearchBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.broker = PiChannelBroker()
        self.broker.model = 'xai/grok-4.6'
        self.broker.thinking = 'low'
        self.broker.workspace = Path(self.temp.name)
        self.broker._authorize_research = Mock()
        self.children = []
        self.spawned = asyncio.Event()
        self.payload = {'systemPrompt': 'Research test', 'messages': [], 'tools': []}
        self.child_output = {'ok': True, 'message': message()}
        self.child_sleep = False
        self.child_large = False
        self.spawn_args = []
        real_spawn = asyncio.create_subprocess_exec

        async def spawn(*args, **kwargs):
            self.spawn_args.append(args)
            self.assertEqual(args[0], '/usr/bin/node')
            self.assertTrue(args[-1].endswith('/research_completion.mjs'))
            self.assertNotIn('/usr/bin/pi', args)
            if self.child_sleep:
                code = 'import sys,time; sys.stdin.buffer.read(); time.sleep(60)'
            elif self.child_large:
                code = f'import sys; sys.stdin.buffer.read(); sys.stdout.write("x"*{MAX_MESSAGE_BYTES + 1})'
            else:
                encoded = json.dumps(self.child_output, separators=(',', ':'))
                if len(encoded) > 120000:
                    fixture = json.loads(encoded)
                    repeat = len(fixture['message']['content'][0]['text'])
                    self.assertEqual(fixture['message']['content'][0]['text'], 'x' * repeat)
                    fixture['message']['content'][0]['text'] = ''
                    small = json.dumps(fixture, separators=(',', ':'))
                    code = (f'import json,sys; sys.stdin.buffer.read(); r=json.loads({small!r}); '
                            f'r["message"]["content"][0]["text"]="x"*{repeat}; '
                            'sys.stdout.write(json.dumps(r,separators=(",",":")))')
                else:
                    code = f'import sys; sys.stdin.buffer.read(); sys.stdout.write({encoded!r})'
            child = await real_spawn(sys.executable, '-c', code, **kwargs)
            self.children.append(child)
            self.spawned.set()
            return child

        self.spawn_patch = patch('mini_hermes.pi_channel.broker.asyncio.create_subprocess_exec', side_effect=spawn)
        self.spawn_patch.start()
        self.socket = Path(self.temp.name) / 'pi.sock'
        self.server = await asyncio.start_unix_server(self.broker._handle, path=self.socket, limit=MAX_MESSAGE_BYTES + 1)
        self.client = PiChannelClient(self.socket, timeout_seconds=3)

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        for child in self.children:
            await self.broker._terminate_research(child)
        self.spawn_patch.stop()
        self.temp.cleanup()

    async def test_model_only_completion_preserves_native_message(self):
        native = message('x' * 100000)
        native['content'][0]['textSignature'] = 'opaque-native-signature'
        self.child_output['message'] = native
        self.assertEqual(await self.client.research_completion(self.payload), native)
        self.assertEqual(len(self.spawn_args), 1)
        self.assertFalse(self.broker._research_processes)

    async def test_no_credentials_or_options_can_be_added_to_request(self):
        for name in ['apiKey', 'headers', 'baseUrl', 'env', 'model', 'samplingParams']:
            with self.subTest(name=name), self.assertRaisesRegex(PiChannelError, 'invalid research request'):
                await self.client.research_completion({**self.payload, name: 'ignored'})
        self.assertFalse(self.spawn_args)

    async def test_model_mismatch_fails_without_fallback_or_subprocess(self):
        self.broker.model = 'ollama-cloud/deepseek-v4-pro'
        with self.assertRaisesRegex(PiChannelError, '配置不一致'):
            await self.client.research_completion(self.payload)
        self.assertFalse(self.spawn_args)

    async def test_upstream_error_is_fixed_and_does_not_echo_secret(self):
        self.child_output = {'ok': False, 'error_code': 'unexpected-secret', 'errorMessage': 'TEST_SECRET_NEVER_ECHO'}
        with self.assertRaises(PiChannelError) as caught:
            await self.client.research_completion(self.payload)
        self.assertNotIn('TEST_SECRET', str(caught.exception))
        self.assertNotIn('unexpected-secret', str(caught.exception))
        self.assertIn('Grok 服务暂时不可用', str(caught.exception))

    async def test_auth_failure_is_actionable(self):
        self.child_output = {'ok': False, 'error_code': 'auth_required'}
        with self.assertRaisesRegex(PiChannelError, '重新授权'):
            await self.client.research_completion(self.payload)

    async def test_deadline_terminates_only_research_child(self):
        self.child_sleep = True
        sentinel = object()
        self.broker._process = sentinel
        with patch('mini_hermes.pi_channel.broker.RESEARCH_TIMEOUT_SECONDS', 0.08):
            with self.assertRaisesRegex(PiChannelError, '研究请求超时'):
                await self.client.research_completion(self.payload)
        self.assertIs(self.broker._process, sentinel)
        self.assertTrue(self.children)
        self.assertIsNotNone(self.children[0].returncode)
        self.assertFalse(self.broker._research_processes)

    async def test_client_disconnect_terminates_child(self):
        self.child_sleep = True
        reader, writer = await asyncio.open_unix_connection(self.socket)
        writer.write((json.dumps({'action': 'research_completion', 'request': self.payload}) + '\n').encode())
        await writer.drain()
        await asyncio.wait_for(self.spawned.wait(), 2)
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(self.children[0].wait(), 2)
        self.assertIsNotNone(self.children[0].returncode)

    async def test_cancelled_client_does_not_leave_model_running(self):
        self.child_sleep = True
        request = asyncio.create_task(self.client.research_completion(self.payload))
        await asyncio.wait_for(self.spawned.wait(), 2)
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request
        await asyncio.wait_for(self.children[0].wait(), 2)

    async def test_response_size_limit_terminates_child(self):
        self.child_large = True
        with self.assertRaisesRegex(PiChannelError, 'Grok 服务暂时不可用'):
            await self.client.research_completion(self.payload)
        self.assertIsNotNone(self.children[0].returncode)

    async def test_request_size_limit_is_checked_before_connecting(self):
        with patch.object(self.client, '_connect', new_callable=AsyncMock) as connect:
            with self.assertRaisesRegex(PiChannelError, 'size limit'):
                await self.client.research_completion({'systemPrompt': 'x' * MAX_MESSAGE_BYTES})
        connect.assert_not_called()

    async def test_final_envelope_cannot_exceed_response_limit(self):
        self.child_output = {'ok': True, 'message': message('')}
        overhead = len(json.dumps(self.child_output, separators=(',', ':')).encode())
        self.child_output['message']['content'][0]['text'] = 'x' * (MAX_MESSAGE_BYTES - overhead - 8)
        with self.assertRaisesRegex(PiChannelError, '长度限制'):
            await self.client.research_completion(self.payload)

    async def test_status_remains_usable_and_reports_research_count(self):
        result = await self.client.status()
        self.assertEqual(result['research_active'], 0)
        self.assertEqual(result['model'], 'xai/grok-4.6')


if __name__ == '__main__':
    unittest.main()
