import os
import sys
import threading
from pathlib import Path
from typing import Optional
import importlib.util

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# Import app-gpu.py which has a hyphen in its name
current_dir = os.path.dirname(os.path.abspath(__file__))

# Ensure ffmpeg from main project is in PATH for librosa/audioread
ffmpeg_path = os.path.abspath(os.path.join(current_dir, "..", "third_party", "ffmpeg", "bin"))
if os.path.exists(ffmpeg_path) and ffmpeg_path not in os.environ.get("PATH", ""):
    os.environ["PATH"] = ffmpeg_path + os.pathsep + os.environ.get("PATH", "")

spec = importlib.util.spec_from_file_location("app_gpu", os.path.join(current_dir, "app-gpu.py"))
app_gpu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_gpu)

GPUASREngine = app_gpu.GPUASREngine

app = FastAPI(title="QwenASRMiniTool API")

engine = GPUASREngine()
engine_lock = threading.Lock()

class TranscribeRequest(BaseModel):
    audio_path: str
    language: Optional[str] = None
    context: Optional[str] = None
    diarize: bool = False
    n_speakers: Optional[int] = None
    simplified: bool = False

class EditorRequest(BaseModel):
    srt_path: str
    audio_path: str

@app.on_event("startup")
def startup_event():
    print("API Server starting. Models will be loaded on the first request.")

def _ensure_engine_loaded():
    with engine_lock:
        if not engine.ready:
            print("Loading models...")
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
            engine.load(device=device, model_dir=app_gpu.GPU_MODEL_DIR, use_aligner=True)
            print("Models loaded.")

@app.post("/api/transcribe")
def transcribe(req: TranscribeRequest):
    audio_file = Path(req.audio_path)
    if not audio_file.exists():
        raise HTTPException(status_code=404, detail="Audio file not found")
        
    app_gpu._g_output_simplified = req.simplified
    
    _ensure_engine_loaded()
    
    try:
        srt_path = engine.process_file(
            audio_path=audio_file,
            language=req.language,
            context=req.context,
            diarize=req.diarize,
            n_speakers=req.n_speakers,
        )
        if not srt_path or not srt_path.exists():
            return {"srt_content": ""}
            
        srt_content = srt_path.read_text(encoding="utf-8")
        return {"srt_content": srt_content}
    except Exception as e:
        import traceback
        err_msg = traceback.format_exc()
        traceback.print_exc()
        with open(os.path.join(current_dir, "api_server_error.log"), "a", encoding="utf-8") as f:
            f.write(err_msg + "\n")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/editor")
def open_editor(req: EditorRequest):
    srt_file = Path(req.srt_path)
    audio_file = Path(req.audio_path)
    if not srt_file.exists():
        raise HTTPException(status_code=404, detail="SRT file not found")
        
    import subprocess
    script = f"""
import tkinter as tk
from subtitle_editor import SubtitleEditorWindow
from pathlib import Path

root = tk.Tk()
root.withdraw() # hide root
window = SubtitleEditorWindow(root, Path(r"{str(srt_file)}"), Path(r"{str(audio_file)}"))
root.mainloop()
"""
    try:
        # spawn the process
        subprocess.Popen(["python", "-c", script], cwd=current_dir)
        return {"status": "Editor launched"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8001)
