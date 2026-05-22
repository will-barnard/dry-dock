# Microsoft's Playwright image ships Chromium + all the system libraries it
# needs, already matched to the Playwright version — far less fragile than
# installing browser deps by hand on a slim base.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Internal-only service; the backend reaches it over the compose network.
EXPOSE 3000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3000"]
