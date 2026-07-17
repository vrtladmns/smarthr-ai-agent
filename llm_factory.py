from langchain_ollama import ChatOllama

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


def make_chat_model(json_mode: bool = False, max_tokens: int | None = None):
    provider = LLM_PROVIDER.lower()

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
        )

    if provider == "ollama":
        kwargs = {
            "model": OLLAMA_CHAT_MODEL,
            "temperature": 0,
            "num_ctx": OLLAMA_NUM_CTX,
            "num_predict": max_tokens or OLLAMA_NUM_PREDICT,
            "keep_alive": "15m",
        }
        if json_mode:
            kwargs["format"] = "json"
        return ChatOllama(**kwargs)

    raise RuntimeError(f"Unsupported LLM_PROVIDER={LLM_PROVIDER!r}. Use 'deepseek' or 'ollama'.")
