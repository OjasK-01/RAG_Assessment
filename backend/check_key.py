"""Smoke test for LLM_API_KEY: one tiny chat call + one tiny embedding call (~10 tokens total)."""
import sys

from openai import OpenAI

from app.config import settings as s

client = OpenAI(api_key=s.llm_api_key, base_url=s.base_url, max_retries=0)
print(f"provider={s.provider} chat={s.chat_model} embed={s.embed_model}")

try:
    r = client.chat.completions.create(
        model=s.chat_model,
        messages=[{"role": "user", "content": "hi"}],
        **({"max_completion_tokens": 3} if s.provider == "openai" else {"max_tokens": 3}),
    )
    print(f"chat  OK -> {r.choices[0].message.content!r} ({r.usage.total_tokens} tokens)")

    e = client.embeddings.create(model=s.embed_model, input="hi", dimensions=s.embed_dim)
    print(f"embed OK -> dim={len(e.data[0].embedding)}")
except Exception as e:
    sys.exit(f"FAIL: {type(e).__name__}: {str(e)[:300]}")
