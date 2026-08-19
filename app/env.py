"""
Environment variable access.

Every value this app reads is something a human pastes into a hosting
dashboard (Vercel's env var UI) or a .env file, and pasting is how trailing
whitespace gets in. That whitespace is invisible in the UI but far from
harmless: an API key with a trailing newline makes an illegal HTTP header
value, and the OpenAI SDK surfaces the resulting h11 rejection as a bare
"Connection error." -- indistinguishable from the network being down, which
made it by far the hardest-to-diagnose failure this app has hit in
production (every LLM verdict failed while feeds fetched fine).

So nothing reads os.environ directly any more; it all goes through here and
gets stripped.
"""
import os


def env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default
