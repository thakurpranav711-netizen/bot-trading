# 1️⃣ Base image (lightweight Python)
FROM python:3.11-slim

# 2️⃣ Set working directory inside container
WORKDIR /app

# 3️⃣ System dependencies (optional but safe)
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 4️⃣ Copy requirements first (better caching)
COPY requirements.txt .

# 5️⃣ Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# 6️⃣ Copy entire project
COPY . .

# 7️⃣ Environment variables (runtime pe override honge)
ENV PYTHONUNBUFFERED=1

# 8️⃣ Run the bot
CMD ["python", "app/main.py"]