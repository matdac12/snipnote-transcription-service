from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from transcribe import transcribe_audio

app = FastAPI(title="SnipNote Transcription Service")

# Allow all origins for testing (will restrict later)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def health_check():
    return {"status": "healthy", "service": "snipnote-transcription"}

@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...)):
    try:
        # Read audio file
        audio_data = await file.read()

        # Transcribe
        result = transcribe_audio(audio_data, file.filename)

        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
