import os

from config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MAX_TOKENS,
    DEEPSEEK_MODEL,
    LLM_PROVIDER,
    OLLAMA_CHAT_MODEL,
    OLLAMA_NUM_CTX,
    OLLAMA_NUM_PREDICT,
)


# Without an explicit timeout the OpenAI client waits 600 seconds per attempt,
# and retries on top of that. On a live voice interview that reads as "processing
# your answer" and never coming back.
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "60"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "1"))


def make_chat_model(
    json_mode: bool = False,
    max_tokens: int | None = None,
    timeout: float | None = None,
    max_retries: int | None = None,
):
    """Build the chat model.

    timeout and max_retries are worth setting explicitly wherever someone is
    waiting: a turn in a voice interview needs to fail fast and be recovered
    from, while a background email decision can afford to wait and retry.
    """
    provider = LLM_PROVIDER.lower()
    request_timeout = LLM_TIMEOUT_SECONDS if timeout is None else timeout
    retries = LLM_MAX_RETRIES if max_retries is None else max_retries

    if provider == "deepseek":
        if not DEEPSEEK_API_KEY:
            raise RuntimeError("DEEPSEEK_API_KEY is required when LLM_PROVIDER=deepseek.")

        from langchain_openai import ChatOpenAI

        model_kwargs = {}
        if json_mode:
            model_kwargs["response_format"] = {"type": "json_object"}

        return ChatOpenAI(
            model=DEEPSEEK_MODEL,
            api_key=DEEPSEEK_API_KEY,
            base_url=DEEPSEEK_BASE_URL,
            temperature=0,
            max_tokens=max_tokens or DEEPSEEK_MAX_TOKENS,
            model_kwargs=model_kwargs,
            timeout=request_timeout,
            max_retries=retries,
        )

    if provider == "ollama":
        from langchain_ollama import ChatOllama

        kwargs = {
            "model": OLLAMA_CHAT_MODEL,
            "temperature": 0,
            "num_ctx": OLLAMA_NUM_CTX,
            "num_predict": max_tokens or OLLAMA_NUM_PREDICT,
            "keep_alive": "15m",
        }
        if json_mode:
            kwargs["format"] = "json"
        # Not every langchain-ollama release accepts a timeout; losing it is
        # better than failing to build the model at all.
        try:
            return ChatOllama(timeout=request_timeout, **kwargs)
        except TypeError:
            return ChatOllama(**kwargs)

    raise RuntimeError(f"Unsupported LLM_PROVIDER={LLM_PROVIDER!r}. Use 'deepseek' or 'ollama'.")
