# AutoVideo Studio

Local / self-hosted AI **text to video** and **text to speech** maker for kids entertainment, stories, education, and social clips.

## Run locally (recommended)

```bat
start.bat
```

Or:

```bash
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:7860/

Copy `.env.example` to `.env` and set `PIXABAY_API_KEY`.

## Why Vercel shows `500 FUNCTION_INVOCATION_FAILED`

This app is a long-running Flask + ffmpeg video builder. [Vercel](https://vercel.com/docs/errors/FUNCTION_INVOCATION_FAILED) runs short serverless functions. Common failure causes:

1. Writing an `outputs/` folder next to the app (Vercel’s disk is read-only except `/tmp`)
2. Video encode longer than the function time limit
3. Windows-only voiceover (SAPI) does not exist on Linux
4. In-memory background jobs do not survive across serverless instances

The repo includes a Vercel-safe output path and `vercel.json`, so the **page can load**. Full video creation is still a poor fit for Vercel Hobby timeouts.

**Better hosts for real video builds:** Railway, Render, Fly.io, or any always-on Linux VM with Python + ffmpeg.

On Vercel, set environment variable `PIXABAY_API_KEY` in the project settings (do not commit `.env`).
