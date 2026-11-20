import uvicorn

from src.core.config import Settings

if __name__ == "__main__":
    uvicorn.run("src.api.main:app", host="127.0.0.1", port=Settings().api_port)
