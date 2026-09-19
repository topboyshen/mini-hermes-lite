"""Explicit native pi RPC regression. Run only in a network-isolated sandbox.

Uses a synthetic extension-supplied summary and fake API key; never a real LLM.
"""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from mini_hermes.pi_channel.broker import PiChannelBroker
from mini_hermes.pi_channel.context_maintenance import context_tokens


# Tests exercise the deployed contract: the unit declares the model explicitly.
os.environ.setdefault("PI_CHANNEL_MODEL", "xai/grok-4.6")


async def main():
    assert os.readlink('/proc/self/ns/net') != os.environ['PI_NATIVE_HOST_NET_NAMESPACE']
    package = Path('/usr/lib/node_modules/@earendil-works/pi-coding-agent/package.json')
    assert json.loads(package.read_text())['version'] == '0.85.1'
    with tempfile.TemporaryDirectory(prefix='native-context-') as directory:
        root = Path(directory)
        agent = root / 'agent'
        agent.mkdir()
        session = root / 'synthetic.jsonl'
        stamp = datetime.now(timezone.utc).isoformat()
        entries = [{'type': 'session', 'version': 3, 'id': 'synthetic-offline', 'timestamp': stamp, 'cwd': directory}]
        parent = None
        usage = {'input': 93000, 'output': 1, 'cacheRead': 0, 'cacheWrite': 0,
                 'totalTokens': 93001, 'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0, 'total': 0}}
        for index, message in enumerate([
            {'role': 'user', 'content': 'old context ' * 12000, 'timestamp': 1},
            {'role': 'assistant', 'content': [{'type': 'text', 'text': 'old answer'}],
             'provider': 'xai', 'model': 'grok-4.6', 'api': 'openai-responses',
             'stopReason': 'stop', 'usage': usage, 'timestamp': 2},
            {'role': 'user', 'content': 'recent context ' * 6000 + 'recent question', 'timestamp': 3},
            {'role': 'assistant', 'content': [{'type': 'text', 'text': 'recent answer'}],
             'provider': 'xai', 'model': 'grok-4.6', 'api': 'openai-responses',
             'stopReason': 'stop', 'usage': usage, 'timestamp': 4},
        ]):
            identity = f'{index + 1:08x}'
            entries.append({'type': 'message', 'id': identity, 'parentId': parent,
                            'timestamp': stamp, 'message': message})
            parent = identity
        session.write_text(''.join(json.dumps(entry) + '\n' for entry in entries))
        original = session.read_bytes()
        extension = root / 'synthetic-summary.mjs'
        extension.write_text('''export default function(pi) {
          pi.on("session_before_compact", async (event) => ({compaction: {
            summary: "Synthetic offline summary. Preserve the recent question.",
            firstKeptEntryId: event.preparation.firstKeptEntryId,
            tokensBefore: event.preparation.tokensBefore
          }}));
        }''')
        broker = PiChannelBroker()
        broker.model, broker.thinking = 'xai/grok-4.6', 'low'
        broker.workspace = root
        broker._session_path = lambda user_id: session
        events = []
        async def send(writer, event):
            events.append(event)
        broker._send = send
        real_spawn = asyncio.create_subprocess_exec
        rpc_errors = []
        async def isolated_spawn(*command, **kwargs):
            # Explicit test-only extension replaces summary generation, not RPC
            # handling or session storage. Discovery remains disabled.
            kwargs['env'] = {**os.environ, 'PI_CODING_AGENT_DIR': str(agent),
                             'XAI_API_KEY': 'TEST'}
            process = await real_spawn(*command, '--extension', str(extension), **kwargs)
            native_reader = process.stdout
            class TracedReader:
                async def readline(self):
                    raw = await native_reader.readline()
                    if raw:
                        event = json.loads(raw)
                        if event.get('type') == 'response' and event.get('success') is False:
                            rpc_errors.append(event.get('error', '')[:200])
                    return raw
            process.stdout = TracedReader()
            return process
        with patch('asyncio.create_subprocess_exec', isolated_spawn):
            result = await broker._maintain_context(42, object())
        assert result['status'] == 'compacted', (result, rpc_errors)
        after = session.read_bytes()
        assert after.startswith(original), 'native compaction must retain the entire original file'
        saved = [json.loads(line) for line in after.splitlines()]
        assert sum(entry['type'] == 'message' for entry in saved) == 4
        assert sum(entry['type'] == 'compaction' for entry in saved) == 1
        assert context_tokens(session) is None
        assert result['tokens_after'] < result['tokens_before']
        assert not any('summary' in event for event in events)
        print(json.dumps({'native_rpc_compaction': 'pass', 'original_history_preserved': True,
                          'real_model_called': False, **result}), flush=True)


asyncio.run(main())
