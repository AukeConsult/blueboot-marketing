import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
import _pathsetup
from functions.config import cfg
from openai import OpenAI

client = OpenAI(api_key=cfg.OPENAI_API_KEY)
r = client.chat.completions.create(
    model=cfg.OPENAI_MODEL,
    messages=[{'role': 'user', 'content': 'Reply with just OK'}],
    max_completion_tokens=5
)
print(f"  Model:    {cfg.OPENAI_MODEL}")
print(f"  Response: {r.choices[0].message.content.strip()}")
