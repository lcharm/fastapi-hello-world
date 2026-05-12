from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def read_root():
    return {"status": "大脑在线", "message": "Hello World from your Electron backend!"}