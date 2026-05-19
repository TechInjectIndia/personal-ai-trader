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
  ],
};
