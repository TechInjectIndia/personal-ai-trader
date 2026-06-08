module.exports = {
  apps: [
    {
      name: "helm-dashboard",
      cwd: "/home/ubuntu/work/personal-ai-trader",
      script: "/home/ubuntu/work/personal-ai-trader/.venv/bin/streamlit",
      args: [
        "run",
        "helm/dashboard/app.py",
        "--server.port=8501",
        "--server.address=127.0.0.1",
        "--server.headless=true",
        "--server.enableXsrfProtection=true",
        "--browser.gatherUsageStats=false",
      ],
      interpreter: "none",
      env: {
        PYTHONUNBUFFERED: "1",
      },
      autorestart: true,
      max_restarts: 10,
      time: true,
    },
    {
      // Context Engine microservice (FastAPI/uvicorn) on 127.0.0.1:8601.
      // Decoupled from the bot loop; the bot reaches it over localhost HTTP
      // only, fail-open + flag-gated (CONTEXT_ENGINE_URL in .env). cwd=repo
      // root makes the top-level `context_engine` package importable (like
      // `helm`). `pm2 save` after start so it survives reboot.
      name: "helm-context-engine",
      cwd: "/home/ubuntu/work/personal-ai-trader",
      script: "/home/ubuntu/work/personal-ai-trader/.venv/bin/uvicorn",
      args: [
        "context_engine.app:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8601",
        "--workers",
        "1",
      ],
      interpreter: "none",
      env: {
        PYTHONUNBUFFERED: "1",
      },
      autorestart: true,
      max_restarts: 10,
      time: true,
    },
  ],
};
