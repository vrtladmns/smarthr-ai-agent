from langchain_ollama import OllamaEmbeddings

from langchain_community.vectorstores import Chroma
from langchain_core.runnables import RunnableConfig
from langsmith import traceable

from langgraph.graph import (
    StateGraph,
    END
)

from typing import TypedDict
from uuid import uuid4

from config import (
    CHROMA_PERSIST_DIR,
    OLLAMA_EMBED_MODEL,
    OLLAMA_NUM_PREDICT,
    RAG_TOP_K,
)
from llm_factory import make_chat_model



class State(TypedDict):
    question:str
    context:str
    answer:str



embeddings = OllamaEmbeddings(
    model=OLLAMA_EMBED_MODEL
)


db = Chroma(
    persist_directory=CHROMA_PERSIST_DIR,
    embedding_function=embeddings
)


llm = make_chat_model(max_tokens=OLLAMA_NUM_PREDICT)



def retrieve(state):

    docs = db.similarity_search(
        state["question"],
        k=RAG_TOP_K
    )

    context="\n\n".join(
        [
            d.page_content
            for d in docs
        ]
    )


    return {
        "context":context
    }



def answer(state):

    prompt=f"""

You are a Company Policy Assistant.

Rules:
- Answer only using the policy context.
- Do not guess.
- Keep the answer concise, preferably 1-3 short sentences.
- If the answer is not present say:
"I could not find this information in company policy."

Policy:

{state['context']}


Question:

{state['question']}

"""


    response=llm.invoke(prompt)


    return {
        "answer":response.content
    }




graph=StateGraph(State)


graph.add_node(
    "retrieve",
    retrieve
)

graph.add_node(
    "answer",
    answer
)


graph.set_entry_point(
    "retrieve"
)


graph.add_edge(
    "retrieve",
    "answer"
)


graph.add_edge(
    "answer",
    END
)


agent=graph.compile()


@traceable(name="company_policy_rag_turn")
def ask_policy(question: str, thread_id: str | None = None) -> str:
    result=agent.invoke(
        {
            "question":question
        },
        config=RunnableConfig(
            run_name="company_policy_rag",
            configurable={
                "thread_id":thread_id or str(uuid4())
            },
            tags=[
                "rag",
                "company-policy"
            ]
        )
    )

    return result["answer"]


def chat_loop():
    thread_id=str(uuid4())

    while True:

        q=input("\nEmployee: ")

        if q.lower() in {"exit", "quit"}:
            break

        answer_text=ask_policy(
            q,
            thread_id=thread_id
        )

        print(
            "\nAI:",
            answer_text
        )


if __name__ == "__main__":
    chat_loop()
