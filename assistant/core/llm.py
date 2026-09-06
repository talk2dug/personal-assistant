"""Thin wrapper around the Ollama client, pointed at simrig's LAN endpoint."""
import ollama


class LLMClient:
    def __init__(self, host: str, model: str):
        self._client = ollama.Client(host=host)
        self.model = model

    def chat(self, messages: list[dict], tools: list[dict] | None = None, think: bool = False) -> dict:
        """Returns the raw assistant message dict (may contain tool_calls).

        think=False skips Qwen3's internal reasoning pass — fine and much faster for the
        small reminder-only tool set. But with Era's ~51 tools added to the mix, Qwen3
        stops reliably using Ollama's structured tool_calls and instead writes a tool
        call as plain text in content — confirmed by testing directly against simrig.
        Callers with a large/complex tool set (Era) should pass think=True; the extra
        latency there is an acceptable tradeoff since finance queries aren't the hot
        low-latency path reminders are.
        keep_alive keeps the model resident in VRAM instead of unloading after Ollama's
        default 5-minute idle timeout, so a message after a lull doesn't pay reload cost.
        """
        # num_ctx: without this, Ollama silently falls back to its own conservative default
        # runtime context (independent of what the model actually supports — gemma4 supports
        # 262144) and truncates from the front of the conversation once exceeded. Confirmed
        # this happening for real: a multi-search email scan's tool results pushed past the
        # untruncated default, and the model's next turn read only a fragment, concluded no
        # user message had been sent yet, and answered nonsense (once a generic greeting, twice
        # a fabricated, unrelated add_reminder call — see the 2026-09-03 mail-scan incident).
        # 32768 comfortably covers system prompt + tool schemas + history + several tool
        # results without meaningfully increasing VRAM use on simrig's 16GB card.
        #
        # temperature: Ollama's default for this model is 1.0 (fairly high/creative), which is
        # fine for prose but genuinely unreliable for tool-use decisions — confirmed directly:
        # with real conversation history in context, gemma4 fabricated "I don't have access to
        # your smart home" (with working Home Assistant tools right there in its tool list)
        # repeatedly at temperature 1.0, and still intermittently at 0.3 once the conversation
        # got long/varied enough (also caught it fabricating success on unrelated finance
        # requests it had no tool for at all — see the 2026-09-03 incidents). Dropped to 0.15,
        # near-deterministic, for tool-use reliability. A tool-calling agent that can touch real
        # bills, locks, and appliances should err hard toward consistency over creative flair.
        response = self._client.chat(
            model=self.model, messages=messages, tools=tools or [], think=think, keep_alive="24h",
            options={"num_ctx": 32768, "temperature": 0.15},
        )
        return response["message"]
