from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import Chroma
from langchain_ollama import OllamaEmbeddings


loader = PyPDFLoader(
    "documents/company_policy.pdf"
)

documents = loader.load()


splitter = RecursiveCharacterTextSplitter(
    chunk_size=500,
    chunk_overlap=50
)


chunks = splitter.split_documents(
    documents
)


embeddings = OllamaEmbeddings(
    model="nomic-embed-text"
)


db = Chroma.from_documents(
    chunks,
    embeddings,
    persist_directory="db"
)


print(
    "Policy indexed successfully"
)