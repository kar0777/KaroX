import importlib.util
import json
import time
from pathlib import Path

TRANSCRIPT = Path.home() / '.claude' / 'projects' / 'D----------aqurium' / '6ed6db9b-4c13-4c30-81e5-86005f7ef4af.jsonl'
PROXY = Path.home() / '.config' / 'ze-claude-proxy' / 'ze_anthropic_proxy.py'

prompt = ''
for line in TRANSCRIPT.read_text(encoding='utf-8', errors='replace').splitlines():
    try:
        obj = json.loads(line)
    except Exception:
        continue
    msg = obj.get('message')
    content = msg.get('content') if isinstance(msg, dict) else None
    if obj.get('type') == 'user' and isinstance(content, str) and len(content) > 1000:
        prompt = content
        break

spec = importlib.util.spec_from_file_location('zp', str(PROXY))
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)
route_id = proxy.load_route_ids()[0]
tools = [
    {
        'name': f'tool_{i}',
        'description': 'coding workspace tool',
        'input_schema': {
            'type': 'object',
            'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}},
            'additionalProperties': True,
        },
    }
    for i in range(59)
]

def probe(effort: str):
    body = {
        'model': 'claude-opus-5',
        'max_tokens': 128000,
        'stream': True,
        'thinking': {'type': 'adaptive'},
        'output_config': {'effort': effort},
        'messages': [{'role': 'user', 'content': prompt}],
        'tools': tools,
    }
    payload = proxy.translate_messages(body)
    payload['stream'] = True
    payload['metadata'] = {'user_id': route_id}
    started = time.monotonic()
    kind, detail, events = 'empty', '', 0
    try:
        response = proxy.call_ze(payload, timeout=30, user_agent=f'ZE-Exact-Effort-{effort}')
        proxy.set_stream_read_timeout(response, 75)
        with response:
            for event in proxy.iter_openai_sse(response):
                events += 1
                choices = event.get('choices') or [{}]
                choice = choices[0] if choices else {}
                delta = choice.get('delta') if isinstance(choice, dict) else {}
                delta = delta if isinstance(delta, dict) else {}
                if delta.get('tool_calls'):
                    kind = 'tool'
                    detail = str((delta.get('tool_calls') or [{}])[0].get('function', {}).get('name') or '')
                    break
                text = delta.get('content')
                if isinstance(text, str) and text:
                    kind = 'text'
                    detail = text[:80]
                    break
    except Exception as exc:
        kind = 'error'
        detail = type(exc).__name__
    return {
        'effort': effort,
        'prompt_chars': len(prompt),
        'kind': kind,
        'seconds': round(time.monotonic() - started, 2),
        'events': events,
        'detail': detail,
    }

if __name__ == '__main__':
    import sys
    effort = sys.argv[1] if len(sys.argv) > 1 else 'medium'
    print(json.dumps(probe(effort), ensure_ascii=False))
